import { expect, test, type Page } from "@playwright/test";
import { clerk, setupClerkTestingToken } from "@clerk/testing/playwright";

/**
 * Each test here checks the number and kind of network requests the
 * experiment page makes. Each assertion encodes a bug that was measured
 * in production and then fixed:
 *
 *  1. Loading the page issues exactly one task-shells request. The shells
 *     used to be fetched twice, once during the server render and once
 *     again on mount.
 *  2. Opening a trial issues exactly one GET /api/trials/{id}. The drawer
 *     and the analysis card each used to fetch the same trial separately.
 *  3. No task-files request uses stream=1 during this flow. Opening a
 *     trial used to download the task's entire file contents behind a
 *     pane that showed only the overview. No stream request of any kind
 *     may happen until a file view is on screen.
 *  4. The trial refetches every few seconds while its analysis is active
 *     and stops once it finishes.
 *
 * Like tasks-view.spec.ts, this needs a running dev stack and Clerk dev
 * credentials, and it skips when they are missing. The experiment needs
 * at least one non-probe trial; set E2E_EXPERIMENT_ID to choose one,
 * otherwise the first experiment on the dashboard is used.
 */

const CLERK_EMAIL = process.env.E2E_CLERK_EMAIL;
const CLERK_SECRET = process.env.CLERK_SECRET_KEY;
const CLERK_PUBLISHABLE = process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY;
const EXPERIMENT_ID = process.env.E2E_EXPERIMENT_ID;

const hasClerkEnv = !!CLERK_EMAIL && !!CLERK_SECRET && !!CLERK_PUBLISHABLE;

const TASK_SHELLS_RE = /\/api\/experiments\/[^/]+\/task-shells/;
// Matches the trial-detail endpoint only. Subpaths like /files,
// /analysis-log, /live, and /trajectory are separate resources and must
// not count here.
const TRIAL_DETAIL_RE = /\/api\/trials\/[^/?]+(\?.*)?$/;
const TASK_FILES_RE = /\/api\/tasks\/[^/]+\/files\?/;
const TASK_FILES_STREAM_RE = /\/api\/tasks\/[^/]+\/files\?[^#]*\bstream=1\b/;
// Requests with stream=1 return every file's contents. Plain listings are
// small and are allowed at any time; for example, the verifier badge
// reads one when a trial opens. Only the stream form has to wait until a
// file view is on screen.
const ANY_FILES_STREAM_RE = /\/files\?[^#]*\bstream=1\b/;
const TRIAL_FILES_STREAM_RE = /\/api\/trials\/[^/]+\/files\?[^#]*\bstream=1\b/;
const TRIAL_TRAJECTORY_RE = /\/api\/trials\/[^/?]+\/trajectory(?:\?.*)?$/;
const TRIAL_SUMMARY_RE = /\/api\/trials\/[^/?]+\/trajectory\/summary(?:\?.*)?$/;

type LoggedRequest = { url: string; method: string };

function recordRequests(page: Page): LoggedRequest[] {
  const log: LoggedRequest[] = [];
  // Only finished requests are counted. In development, React StrictMode
  // runs every effect twice and the first run's fetch gets aborted. Those
  // aborted requests are not real duplicates, so they must not count.
  page.on("requestfinished", (request) =>
    log.push({ url: request.url(), method: request.method() })
  );
  return log;
}

function countSince(
  log: LoggedRequest[],
  from: number,
  re: RegExp,
  method = "GET"
): number {
  return log.slice(from).filter((r) => r.method === method && re.test(r.url))
    .length;
}

test.describe("experiment page network shape", () => {
  test.skip(
    !hasClerkEnv,
    "needs E2E_CLERK_EMAIL + CLERK_SECRET_KEY + NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY"
  );

  test("one fetch per resource, bodies on demand, polling gated on analysis", async ({
    page,
  }) => {
    test.setTimeout(120_000);

    await setupClerkTestingToken({ page });
    await page.goto("/");
    await clerk.signIn({ page, emailAddress: CLERK_EMAIL! });

    const log = recordRequests(page);

    // This rewrites the fetched trial's analysis_status to "running" so
    // the refetching behavior can be tested on any seed data. It is
    // flipped to "success" later in the test to check that the refetching
    // stops. The glob only matches /api/trials/{id} itself, and requests
    // to subpaths pass through unchanged.
    let analysisStatusOverride: string | null = "running";
    await page.route("**/api/trials/*", async (route) => {
      if (route.request().method() !== "GET" || analysisStatusOverride === null)
        return route.fallback();
      const response = await route.fetch();
      const body = (await response.json()) as Record<string, unknown>;
      await route.fulfill({
        response,
        json: {
          ...body,
          analysis_status: analysisStatusOverride,
          analysis: null,
          analysis_error: null,
        },
      });
    });

    // Phase 1 — load the experiment page, let the grid settle.
    if (EXPERIMENT_ID) {
      await page.goto(`/experiments/${encodeURIComponent(EXPERIMENT_ID)}`);
    } else {
      // The /experiments page contains no experiment links; experiments
      // are opened from the dashboard. If this environment has no
      // experiment at all, which is the case for the CI seed, the test
      // skips instead of failing. Failing here would make pull requests
      // fail because of the seed's contents rather than because of
      // anything this spec tests. Setting E2E_EXPERIMENT_ID makes the
      // test strict instead.
      await page.goto("/dashboard");
      const experimentLink = page.locator('a[href^="/experiments/"]').first();
      const hasExperiment = await experimentLink
        .waitFor({ timeout: 15_000 })
        .then(
          () => true,
          () => false
        );
      test.skip(
        !hasExperiment,
        "no experiment to open in this environment — set E2E_EXPERIMENT_ID"
      );
      await experimentLink.click();
    }

    const trialCell = page.getByRole("button", { name: /^Trial \d+/ }).first();
    if (EXPERIMENT_ID) {
      await expect(trialCell).toBeVisible({ timeout: 30_000 });
    } else {
      // The same skip rule applies when the discovered experiment has no
      // trial cells.
      const hasTrialCell = await trialCell.waitFor({ timeout: 30_000 }).then(
        () => true,
        () => false
      );
      test.skip(
        !hasTrialCell,
        "discovered experiment has no non-probe trials — set E2E_EXPERIMENT_ID"
      );
    }

    expect(countSince(log, 0, TASK_SHELLS_RE)).toBe(1);
    expect(countSince(log, 0, TRIAL_DETAIL_RE)).toBe(0);

    // Phase 2 — open a trial. Exactly one detail fetch happens, shared by
    // the drawer and the analysis card. The visible task pane fetches its
    // plain tree listing, and nothing downloads file contents.
    const openMark = log.length;
    await trialCell.click();
    await expect(page.getByRole("tab", { name: "Summary" })).toBeVisible({
      timeout: 15_000,
    });
    await expect
      .poll(() => countSince(log, openMark, TRIAL_DETAIL_RE), {
        timeout: 10_000,
      })
      .toBeGreaterThanOrEqual(1);
    // Less than the 5-second refetch interval has passed, so a second
    // request at this point would mean two components are fetching the
    // same trial, not that refetching started.
    await page.waitForTimeout(1_500);
    expect(countSince(log, openMark, TRIAL_DETAIL_RE)).toBe(1);
    await expect
      .poll(() => countSince(log, openMark, TASK_FILES_RE), {
        timeout: 10_000,
      })
      .toBe(1);
    expect(countSince(log, openMark, ANY_FILES_STREAM_RE)).toBe(0);

    // Phase 3 — the analysis reads as active, so the trial must be
    // refetched on an interval.
    const pollMark = log.length;
    await expect
      .poll(() => countSince(log, pollMark, TRIAL_DETAIL_RE), {
        timeout: 8_000,
      })
      .toBeGreaterThanOrEqual(1);

    // Phase 4 — flip the analysis to success. The next refetch delivers
    // it, and after that no detail request may appear for a full refetch
    // interval.
    analysisStatusOverride = "success";
    const terminalMark = log.length;
    await expect
      .poll(() => countSince(log, terminalMark, TRIAL_DETAIL_RE), {
        timeout: 8_000,
      })
      .toBeGreaterThanOrEqual(1);
    const quietMark = log.length;
    await page.waitForTimeout(6_500);
    expect(countSince(log, quietMark, TRIAL_DETAIL_RE)).toBe(0);

    // Phase 5 — the file-view listing fires only once the Files tab is
    // actually shown. The panel sends this listing with stream=1, and the
    // trial-files endpoint ignores that parameter and answers with a
    // plain listing.
    const filesMark = log.length;
    await page.getByRole("tab", { name: "Files" }).click();
    await expect
      .poll(() => countSince(log, filesMark, TRIAL_FILES_STREAM_RE), {
        timeout: 10_000,
      })
      .toBe(1);

    // Phase 6 — first summary view sees pending, queues exactly one durable
    // job, then polls GET until ready. The immutable ATIF resource still
    // fetches at most once across drawer/layout remounts.
    let summaryGets = 0;
    await page.route("**/api/trials/*/trajectory/summary", async (route) => {
      if (route.request().method() === "POST") {
        return route.fulfill({
          json: { status: "pending", job_id: "summary-1" },
        });
      }
      summaryGets += 1;
      return route.fulfill({
        json:
          summaryGets === 1
            ? { status: "pending" }
            : {
                schema_version: "5",
                model: "test-model",
                generated_at: "2026-08-06T00:00:00Z",
                summary: "Ready",
                highlights: [],
                components: [],
              },
      });
    });
    const trajectoryMark = log.length;
    await page.getByRole("tab", { name: "Trajectory" }).click();
    await expect
      .poll(() => countSince(log, trajectoryMark, TRIAL_SUMMARY_RE, "POST"), {
        timeout: 10_000,
      })
      .toBe(1);
    await expect
      .poll(() => countSince(log, trajectoryMark, TRIAL_SUMMARY_RE), {
        timeout: 10_000,
      })
      .toBe(2);
    const summarySettledMark = log.length;
    await page.waitForTimeout(6_500);
    expect(countSince(log, summarySettledMark, TRIAL_SUMMARY_RE)).toBe(0);
    expect(
      countSince(log, trajectoryMark, TRIAL_TRAJECTORY_RE)
    ).toBeLessThanOrEqual(1);

    // Across the whole journey, the task's file contents were never
    // streamed: every task-files listing must be a plain one.
    expect(countSince(log, 0, TASK_FILES_STREAM_RE)).toBe(0);
  });
});

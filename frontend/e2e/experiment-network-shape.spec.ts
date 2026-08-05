import { expect, test, type Page } from "@playwright/test";
import { clerk, setupClerkTestingToken } from "@clerk/testing/playwright";

/**
 * Network-shape invariants for the experiment page. Each assertion encodes a
 * measured production defect as a regression net:
 *
 *  1. Loading the page issues exactly one task-shells request (the shells
 *     were fetched twice: SSR + a client revalidation of the same URL).
 *  2. Opening a trial issues exactly one GET /api/trials/{id} (the drawer
 *     and the analysis card each ran their own fetch of the same trial).
 *  3. No task-files request carries stream=1 in this flow (opening a trial
 *     streamed the whole task bundle — every file body — behind a pane
 *     showing only the overview), and no body-carrying (stream) listing
 *     of any kind happens until a file view is actually shown.
 *  4. The per-trial subscription polls while analysis_status is active and
 *     stops once it is terminal.
 *
 * Like tasks-view.spec.ts this assumes an already-running dev stack and
 * skips without Clerk credentials. The experiment needs at least one
 * non-probe trial; set E2E_EXPERIMENT_ID to pin one, otherwise the first
 * experiment on /experiments is used.
 */

const CLERK_EMAIL = process.env.E2E_CLERK_EMAIL;
const CLERK_SECRET = process.env.CLERK_SECRET_KEY;
const CLERK_PUBLISHABLE = process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY;
const EXPERIMENT_ID = process.env.E2E_EXPERIMENT_ID;

const hasClerkEnv = !!CLERK_EMAIL && !!CLERK_SECRET && !!CLERK_PUBLISHABLE;

const TASK_SHELLS_RE = /\/api\/experiments\/[^/]+\/task-shells/;
// The trial-detail endpoint only — subpaths (/files, /analysis-log, /live,
// /trajectory) are separate resources and must not count here.
const TRIAL_DETAIL_RE = /\/api\/trials\/[^/?]+(\?.*)?$/;
const TASK_FILES_RE = /\/api\/tasks\/[^/]+\/files\?/;
const TASK_FILES_STREAM_RE = /\/api\/tasks\/[^/]+\/files\?[^#]*\bstream=1\b/;
// Any body-carrying listing (stream=1 asks for every file body). Plain
// listings are cheap and legitimate outside the Files views — e.g. the
// verifier summary reads the trial-files listing on open — so only the
// stream form is gated on a file view being shown.
const ANY_FILES_STREAM_RE = /\/files\?[^#]*\bstream=1\b/;
const TRIAL_FILES_STREAM_RE = /\/api\/trials\/[^/]+\/files\?[^#]*\bstream=1\b/;

type LoggedRequest = { url: string; method: string };

function recordRequests(page: Page): LoggedRequest[] {
  const log: LoggedRequest[] = [];
  // requestfinished, not request: the dev server double-invokes effects
  // (StrictMode), and the first invocation's aborted fetch is React
  // hygiene, not a duplicate the invariants care about.
  page.on("requestfinished", (request) =>
    log.push({ url: request.url(), method: request.method() }),
  );
  return log;
}

function countSince(
  log: LoggedRequest[],
  from: number,
  re: RegExp,
  method = "GET",
): number {
  return log
    .slice(from)
    .filter((r) => r.method === method && re.test(r.url)).length;
}

test.describe("experiment page network shape", () => {
  test.skip(
    !hasClerkEnv,
    "needs E2E_CLERK_EMAIL + CLERK_SECRET_KEY + NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY",
  );

  test("one fetch per resource, bodies on demand, polling gated on analysis", async ({
    page,
  }) => {
    test.setTimeout(120_000);

    await setupClerkTestingToken({ page });
    await page.goto("/");
    await clerk.signIn({ page, emailAddress: CLERK_EMAIL! });

    const log = recordRequests(page);

    // Force the opened trial's analysis to read as active so the polling
    // half of invariant 4 is reachable on any seed data; flipped to
    // "success" later to assert the poll stops. Only the detail endpoint
    // matches the glob — subpath requests fall through untouched.
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
      // /experiments is a placeholder; the dashboard is the experiment
      // entrypoint. Discovery is opportunistic: an org with no experiment
      // to open (e.g. the CI dashboard seed, which provisions one task and
      // no experiments) skips instead of failing — a hard failure here
      // would gate PRs on seed contents, not on the network shape under
      // test. Pin E2E_EXPERIMENT_ID to make this strict.
      await page.goto("/dashboard");
      const experimentLink = page.locator('a[href^="/experiments/"]').first();
      const hasExperiment = await experimentLink.waitFor({ timeout: 15_000 }).then(
        () => true,
        () => false,
      );
      test.skip(
        !hasExperiment,
        "no experiment to open in this environment — set E2E_EXPERIMENT_ID",
      );
      await experimentLink.click();
    }

    const trialCell = page.getByRole("button", { name: /^Trial \d+/ }).first();
    if (EXPERIMENT_ID) {
      await expect(trialCell).toBeVisible({ timeout: 30_000 });
    } else {
      // Same opportunism for a discovered experiment with no trial cells.
      const hasTrialCell = await trialCell.waitFor({ timeout: 30_000 }).then(
        () => true,
        () => false,
      );
      test.skip(
        !hasTrialCell,
        "discovered experiment has no non-probe trials — set E2E_EXPERIMENT_ID",
      );
    }

    expect(countSince(log, 0, TASK_SHELLS_RE)).toBe(1);
    expect(countSince(log, 0, TRIAL_DETAIL_RE)).toBe(0);

    // Phase 2 — open a trial: exactly one detail fetch (shared by the
    // drawer and the analysis card), the visible task pane fetches its
    // plain tree listing, and no trial-files listing yet.
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
    // Inside the 5s poll tick: anything beyond one request here is a
    // duplicate subscriber, not polling.
    await page.waitForTimeout(1_500);
    expect(countSince(log, openMark, TRIAL_DETAIL_RE)).toBe(1);
    await expect
      .poll(() => countSince(log, openMark, TASK_FILES_RE), {
        timeout: 10_000,
      })
      .toBe(1);
    expect(countSince(log, openMark, ANY_FILES_STREAM_RE)).toBe(0);

    // Phase 3 — analysis reads as active, so the subscription must poll.
    const pollMark = log.length;
    await expect
      .poll(() => countSince(log, pollMark, TRIAL_DETAIL_RE), {
        timeout: 8_000,
      })
      .toBeGreaterThanOrEqual(1);

    // Phase 4 — flip to terminal: the next poll delivers it, then polling
    // must stop (no detail request across a full poll interval).
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

    // Phase 5 — the file-view listing (the panel's stream-form request;
    // the trial-files endpoint ignores the param and answers plain) fires
    // only once the Files tab is actually shown.
    const filesMark = log.length;
    await page.getByRole("tab", { name: "Files" }).click();
    await expect
      .poll(() => countSince(log, filesMark, TRIAL_FILES_STREAM_RE), {
        timeout: 10_000,
      })
      .toBe(1);

    // Whole journey: the task bundle (tree + every file body) was never
    // streamed. Task-files listings must all be plain.
    expect(countSince(log, 0, TASK_FILES_STREAM_RE)).toBe(0);
  });
});

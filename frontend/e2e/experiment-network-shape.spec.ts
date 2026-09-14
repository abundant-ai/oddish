import { expect, test } from "@playwright/test";
import { clerk, setupClerkTestingToken } from "@clerk/testing/playwright";

import {
  countSince,
  holdCountedResponses,
  recordRequests,
} from "./network-log";

/**
 * Each test here checks the number and kind of network requests the
 * experiment page makes. Each assertion encodes a bug that was measured
 * in production and then fixed:
 *
 *  1. Loading the page issues exactly one bounded open request. The resource
 *     used to be fetched twice, once during the server render and once
 *     again on mount.
 *  2. Hovering and opening a trial issues one GET /api/trials/{id}. The
 *     preload and drawer share the same SWR cache entry.
 *  3. No task-files request uses stream=1 during this flow. Opening a
 *     trial used to download the task's entire file contents behind a
 *     pane that showed only the overview. No stream request of any kind
 *     may happen until a file view is on screen.
 *  4. Tab resources remain gated on the tab the user selected.
 *
 * Like tasks-view.spec.ts, this needs a running dev stack and Clerk dev
 * credentials, and it skips when they are missing. The experiment needs
 * at least one non-probe trial; set E2E_EXPERIMENT_ID to choose one,
 * otherwise the first experiment on the dashboard is used.
 *
 * Counting is race-hardened by the rules in network-log.ts: only finished
 * requests issued after recording started count, requests are attributed
 * to their issue time, and every counted endpoint's response is held
 * briefly so StrictMode's aborted duplicate can never finish first.
 */

const CLERK_EMAIL = process.env.E2E_CLERK_EMAIL;
const CLERK_SECRET = process.env.CLERK_SECRET_KEY;
const CLERK_PUBLISHABLE = process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY;
const EXPERIMENT_ID = process.env.E2E_EXPERIMENT_ID;

const hasClerkEnv = !!CLERK_EMAIL && !!CLERK_SECRET && !!CLERK_PUBLISHABLE;

const EXPERIMENT_OPEN_RE = /\/api\/experiments\/[^/]+\/open/;
// Matches the trial-detail endpoint only. Subpaths like /files, /live, and
// /trajectory are separate resources and must not count here.
const TRIAL_DETAIL_RE = /\/api\/trials\/[^/?]+(\?.*)?$/;
const TASK_FILES_RE = /\/api\/tasks\/[^/]+\/files\?/;
const TASK_FILES_STREAM_RE = /\/api\/tasks\/[^/]+\/files\?[^#]*\bstream=1\b/;
// Requests with stream=1 return every file's contents. The visible task pane
// may request its plain tree, but trial-file requests wait for the Files or
// Artifacts tab (covered strictly by critical-react-subtree.spec.ts).
const ANY_FILES_STREAM_RE = /\/files\?[^#]*\bstream=1\b/;
const TRIAL_FILES_STREAM_RE = /\/api\/trials\/[^/]+\/files\?[^#]*\bstream=1\b/;

test.describe("experiment page network shape", () => {
  test.skip(
    !hasClerkEnv,
    "needs E2E_CLERK_EMAIL + CLERK_SECRET_KEY + NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY"
  );

  test("trial detail preloads once and tab bodies load on demand", async ({
    page,
  }) => {
    test.setTimeout(120_000);

    await setupClerkTestingToken({ page });
    await page.goto("/");
    await clerk.signIn({ page, emailAddress: CLERK_EMAIL! });

    await holdCountedResponses(page, [
      EXPERIMENT_OPEN_RE,
      TRIAL_DETAIL_RE,
      TASK_FILES_RE,
      TRIAL_FILES_STREAM_RE,
    ]);
    const log = recordRequests(page);

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

    expect(countSince(log, 0, EXPERIMENT_OPEN_RE)).toBe(1);
    expect(countSince(log, 0, TRIAL_DETAIL_RE)).toBe(0);

    // Phase 2 — hover and open a trial. The preload and mounted drawer share
    // one request. The visible task pane fetches its plain tree listing, and
    // nothing downloads file contents.
    const openMark = Date.now();
    await trialCell.hover();
    await trialCell.click();
    await expect(page.getByRole("tab", { name: "Summary" })).toBeVisible({
      timeout: 15_000,
    });
    await expect
      .poll(() => countSince(log, openMark, TRIAL_DETAIL_RE), {
        timeout: 10_000,
      })
      .toBe(1);
    await page.waitForTimeout(1_500);
    expect(countSince(log, openMark, TRIAL_DETAIL_RE)).toBe(1);
    await expect
      .poll(() => countSince(log, openMark, TASK_FILES_RE), {
        timeout: 10_000,
      })
      .toBe(1);
    expect(countSince(log, openMark, ANY_FILES_STREAM_RE)).toBe(0);

    // Phase 3 — the file-view listing fires only once the Files tab is
    // actually shown. The panel sends this listing with stream=1, and the
    // trial-files endpoint ignores that parameter and answers with a
    // plain listing.
    const filesMark = Date.now();
    await page.getByRole("tab", { name: "Files" }).click();
    await expect
      .poll(() => countSince(log, filesMark, TRIAL_FILES_STREAM_RE), {
        timeout: 10_000,
      })
      .toBe(1);

    // Across the whole journey, the task's file contents were never
    // streamed: every task-files listing must be a plain one.
    expect(countSince(log, 0, TASK_FILES_STREAM_RE)).toBe(0);
  });
});

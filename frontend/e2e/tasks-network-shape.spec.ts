import { expect, test, type Route } from "@playwright/test";
import { clerk, setupClerkTestingToken } from "@clerk/testing/playwright";

import { countSince, holdCountedResponses, recordRequests } from "./network-log";

/**
 * Each test here checks the number and kind of network requests the tasks
 * page makes. Each assertion encodes a bug that was measured in a staging
 * HAR (2026-08-06) and then fixed:
 *
 *  1. Loading /tasks issues exactly one browse fetch. The browse call used
 *     to run inside the server render: the document streamed for as long
 *     as the backend query took, nothing painted before it finished, and
 *     the browser could neither see nor reuse the result.
 *  2. Returning to /tasks (client-side) paints the grid from the SWR
 *     cache. The revalidation request is held open while the grid is
 *     checked, so the rows on screen cannot have come from it.
 *  3. Changing a filter issues exactly one more browse fetch — and does
 *     not re-ask for the sidebar's facets or tags.
 *  4. Facets, tags, and the leaderboard are asked once per session, not
 *     once per mount. Each used to be re-fetched by any remount that fell
 *     outside SWR's 5s dedup window (the HAR shows facets ×2, tags ×3,
 *     leaderboard ×2 in one session).
 *
 * Like tasks-view.spec.ts, this needs a running dev stack and Clerk dev
 * credentials, and it skips when they are missing. It needs no particular
 * seed: an org with zero tasks settles on the empty state, and the request
 * counts assert the same way.
 *
 * Counting is race-hardened by the rules in network-log.ts: only finished
 * requests issued after recording started count, requests are attributed
 * to their issue time, and every counted endpoint's response is held
 * briefly so StrictMode's aborted duplicate can never finish first.
 */

const CLERK_EMAIL = process.env.E2E_CLERK_EMAIL;
const CLERK_SECRET = process.env.CLERK_SECRET_KEY;
const CLERK_PUBLISHABLE = process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY;

const hasClerkEnv = !!CLERK_EMAIL && !!CLERK_SECRET && !!CLERK_PUBLISHABLE;

// The browse endpoint itself. /api/tasks/browse/facets and
// /api/tasks/browse/experiment-options are separate resources and must not
// count here — the `?` requires the query form.
const BROWSE_RE = /\/api\/tasks\/browse\?/;
const FACETS_RE = /\/api\/tasks\/browse\/facets/;
const TAGS_RE = /\/api\/tags(\?|$)/;
const LEADERBOARD_RE = /\/api\/leaderboard\?/;

test.describe("tasks page network shape", () => {
  test.skip(
    !hasClerkEnv,
    "needs E2E_CLERK_EMAIL + CLERK_SECRET_KEY + NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY"
  );

  test("one browse fetch per filter state, cached grid on return, vocabularies once per session", async ({
    page,
  }) => {
    test.setTimeout(120_000);

    await setupClerkTestingToken({ page });
    await page.goto("/");
    await clerk.signIn({ page, emailAddress: CLERK_EMAIL! });
    // Let the signed-in landing page's own requests drain so none of them
    // are counted against the journey below. Best effort — client-side
    // beacons may keep the network from ever going fully idle.
    await page
      .waitForLoadState("networkidle", { timeout: 10_000 })
      .catch(() => {});

    await holdCountedResponses(page, [
      BROWSE_RE,
      FACETS_RE,
      TAGS_RE,
      LEADERBOARD_RE,
    ]);
    const log = recordRequests(page);

    // The grid settles on either real rows or the empty state; both come
    // from the browse response.
    const settled = page
      .locator('a[href^="/tasks/"]')
      .first()
      .or(page.getByText("No tasks match the current filters."));

    // Phase 1 — a hard navigation is a fresh JS heap, so this is the
    // session's cold load. Exactly one browse fetch serves the grid.
    await page.goto("/tasks");
    // CardTitle renders a styled <div>, not an <h*>, so this is text — not a
    // heading role.
    await expect(
      page.getByText("Recent Tasks", { exact: true })
    ).toBeVisible();
    await expect(settled.first()).toBeVisible({ timeout: 30_000 });
    await expect
      .poll(() => countSince(log, 0, BROWSE_RE), { timeout: 10_000 })
      .toBe(1);
    // No straggler second fetch: less than the 5s dedup window has passed,
    // so anything arriving now would be a duplicate mount fetching on its
    // own, not a revalidation.
    await page.waitForTimeout(1_500);
    expect(countSince(log, 0, BROWSE_RE)).toBe(1);

    // Phase 2 — leave through the nav (client-side, cache intact) and come
    // back. The grid must paint from the cache: the browse revalidation is
    // held open until after the rows are checked, so they cannot have come
    // from the network.
    const returnMark = Date.now();
    let releaseHeld: (() => void) | undefined;
    const held = new Promise<void>((resolve) => {
      releaseHeld = resolve;
    });
    // Named so the unroute below removes only this handler and leaves the
    // holdCountedResponses route for BROWSE_RE in place.
    const holdBrowseOpen = async (route: Route) => {
      await held;
      await route.fallback();
    };
    await page.route(BROWSE_RE, holdBrowseOpen);
    await page.getByRole("link", { name: "Dashboard" }).first().click();
    await expect(page).toHaveURL(/\/dashboard/);
    await page.getByRole("link", { name: "Tasks" }).first().click();
    await expect(page).toHaveURL(/\/tasks/);
    await expect(settled.first()).toBeVisible({ timeout: 10_000 });
    // Coming back re-mounted the sidebar too — it must not have re-asked
    // for its vocabularies (the dashboard leg is included in this window).
    expect(countSince(log, returnMark, FACETS_RE)).toBe(0);
    expect(countSince(log, returnMark, TAGS_RE)).toBe(0);
    releaseHeld?.();
    await page.unroute(BROWSE_RE, holdBrowseOpen);
    // The return triggers at most one background revalidation — none at
    // all when it lands inside the dedup window.
    await page.waitForTimeout(1_500);
    expect(countSince(log, returnMark, BROWSE_RE)).toBeLessThanOrEqual(1);

    // Phase 3 — a filter change is one more browse fetch (after the 300ms
    // search debounce), and only that: rows are re-asked, vocabularies are
    // not.
    const filterMark = Date.now();
    await page
      .getByPlaceholder("Search anything...")
      .fill("zzz-network-shape-probe");
    await expect
      .poll(() => countSince(log, filterMark, BROWSE_RE), { timeout: 10_000 })
      .toBe(1);
    await page.waitForTimeout(1_500);
    expect(countSince(log, filterMark, BROWSE_RE)).toBe(1);
    expect(countSince(log, filterMark, FACETS_RE)).toBe(0);
    expect(countSince(log, filterMark, TAGS_RE)).toBe(0);

    // Across the whole journey — cold load, dashboard round trip, filter
    // change — each vocabulary was asked exactly once. The leaderboard
    // belongs to the dashboard leg and may legitimately be absent (empty
    // org), but must never repeat.
    expect(countSince(log, 0, FACETS_RE)).toBe(1);
    expect(countSince(log, 0, TAGS_RE)).toBe(1);
    expect(countSince(log, 0, LEADERBOARD_RE)).toBeLessThanOrEqual(1);
  });
});

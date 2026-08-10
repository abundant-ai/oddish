import type { Page, Request } from "@playwright/test";

/**
 * Request-counting helpers shared by the network-shape specs.
 *
 * The specs assert how many requests a page journey makes against a live
 * dev stack, which makes them sensitive to races that have nothing to do
 * with the bugs they guard. Each counting rule here closes one observed
 * flake class:
 *
 *  1. Only finished requests count. In development React StrictMode runs
 *     every mount effect twice and the first run's fetch is aborted by
 *     its cleanup; aborted requests never finish, so they never count.
 *  2. Requests issued before recording started never count, even when
 *     they finish afterwards. The sign-in landing page's requests only
 *     drain best-effort, and a straggler finishing late must not be
 *     attributed to the journey under test.
 *  3. Requests are attributed to the moment they were issued, not the
 *     moment they finished. Phase marks are wall-clock (Date.now()), so
 *     a slow response from one phase cannot drift into a later phase's
 *     window and trip that phase's zero-assertion.
 *
 * Rule 1 has a residual race — the abort only excludes the request if it
 * beats the response, and on localhost the response sometimes wins. Use
 * holdCountedResponses to close it.
 */

export type LoggedRequest = { url: string; method: string; issuedAt: number };

export function recordRequests(page: Page): LoggedRequest[] {
  const log: LoggedRequest[] = [];
  const issuedAt = new Map<Request, number>();
  page.on("request", (request) => issuedAt.set(request, Date.now()));
  page.on("requestfinished", (request) => {
    const at = issuedAt.get(request);
    issuedAt.delete(request);
    if (at === undefined) return; // issued before recording started
    log.push({ url: request.url(), method: request.method(), issuedAt: at });
  });
  return log;
}

/** Count logged requests issued at or after `fromTime` (a Date.now() mark). */
export function countSince(
  log: LoggedRequest[],
  fromTime: number,
  re: RegExp,
  method = "GET"
): number {
  return log.filter(
    (r) => r.issuedAt >= fromTime && r.method === method && re.test(r.url)
  ).length;
}

/**
 * StrictMode's cleanup aborts the first effect's fetch within a few
 * milliseconds of issuing it. Holding every counted response at least
 * this long guarantees the abort wins the race, so the aborted request
 * can never be counted as a finished duplicate. Real duplicate fetches
 * are never aborted — they still finish and still count.
 */
export const STRICT_MODE_ABORT_MS = 250;

export async function holdCountedResponses(
  page: Page,
  patterns: RegExp[]
): Promise<void> {
  for (const pattern of patterns) {
    await page.route(pattern, async (route) => {
      await new Promise((resolve) => setTimeout(resolve, STRICT_MODE_ABORT_MS));
      await route.fallback();
    });
  }
}

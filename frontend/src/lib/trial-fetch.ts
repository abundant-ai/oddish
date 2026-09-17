// Fetch policy for one trial's detail (`/api/trials/{id}`).
//
// The backend marks a trial whose execution and analysis are both terminal
// as cacheable for a day (`backend/api/trial_cache.py`), so a reload or a
// return visit can serve it from the browser's HTTP cache with no request.
// Two client actions make that copy wrong before it expires -- "Re-run
// analysis" on the trial, and a task-level QA run settling onto it -- and
// each of them must go through `markTrialForReload` so the next fetch for
// that key uses `cache: "reload"` and replaces the stored copy. Kept free of
// SWR and React so it can be unit-tested with plain node.

// If a fetch hangs forever, the components using this would show a loading
// state forever. This timeout makes a hung fetch fail like a normal error,
// and the components then fall back to the trial data they already have.
export const TRIAL_FETCH_TIMEOUT_MS = 15_000;

const reloadNext = new Set<string>();

/** The next fetch for `key` bypasses the browser cache and refreshes it. */
export function markTrialForReload(key: string): void {
  reloadNext.add(key);
}

/** Request options for `key`, consuming a pending reload mark if present. */
export function trialRequestInit(key: string): RequestInit {
  const reload = reloadNext.delete(key);
  return {
    ...(reload ? { cache: "reload" as const } : {}),
    signal: AbortSignal.timeout(TRIAL_FETCH_TIMEOUT_MS),
  };
}

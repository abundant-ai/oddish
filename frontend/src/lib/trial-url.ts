/**
 * The `?trial=` address param.
 *
 * A trial id is `{task_id}-{index}`, and every URL that carries a trial also
 * carries its task (as `?task=` or in the `/tasks/{id}` path), so spelling the
 * task id out a second time roughly doubles the address for no information.
 * Links are written with just the index (`?trial=174`).
 *
 * Reading accepts both forms: the short index and the full id that older links
 * — and anything already shared, bookmarked, or pasted into a doc — carry.
 * Trials whose id is not derived from the task in the address (imported rows)
 * keep the full id on the way out too, so they stay resolvable.
 */

const TRIAL_INDEX = /^\d+$/;

/** The value to put in `?trial=`: the bare index when it round-trips. */
export function shortTrialParam(
  trialId: string,
  taskId: string | null | undefined,
): string {
  if (!taskId) return trialId;
  const prefix = `${taskId}-`;
  if (!trialId.startsWith(prefix)) return trialId;
  const index = trialId.slice(prefix.length);
  return TRIAL_INDEX.test(index) ? index : trialId;
}

/** The trial id a `?trial=` value addresses, given the task in the address. */
export function expandTrialParam(
  param: string | null | undefined,
  taskId: string | null | undefined,
): string | null {
  if (!param) return null;
  if (!taskId || !TRIAL_INDEX.test(param)) return param;
  return `${taskId}-${param}`;
}

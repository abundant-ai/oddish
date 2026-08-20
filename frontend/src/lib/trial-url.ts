/**
 * Reading the `?trial=` address param.
 *
 * A trial id is `{task_id}-{index}`, and every URL that carries a trial also
 * carries its task (as `?task=` or in the `/tasks/{id}` path), so the id
 * spells the task out a second time. The app writes that full id — this is
 * the read side only, which additionally accepts the bare index, so a link
 * hand-shortened to `?trial=706` before it goes in a doc or a deck resolves.
 *
 * (#1171 also wrote the short form and #1203 reverted it. Accepting it on
 * read is the half that costs nothing: no link the app mints changes, and
 * short links minted while #1171 was live start resolving again.)
 */

const TRIAL_INDEX = /^\d+$/;

/** The trial id a `?trial=` value addresses, given the task in the address. */
export function expandTrialParam(
  param: string | null | undefined,
  taskId: string | null | undefined
): string | null {
  if (!param) return null;
  if (!taskId || !TRIAL_INDEX.test(param)) return param;
  return `${taskId}-${param}`;
}

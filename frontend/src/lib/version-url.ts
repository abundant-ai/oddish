/**
 * Reading the `?version=` address param.
 *
 * A task version id is `{task_id}-v{n}` and only ever appears on
 * `/tasks/{id}`, so the id spells the task out a second time. The app writes
 * that full id — this is the read side only, which additionally accepts the
 * version number alone (`?version=20` or `?version=v20`), so a link
 * hand-shortened before it goes in a doc or a deck resolves.
 */

const VERSION_NUMBER = /^v?(\d+)$/i;

/** The version id a `?version=` value addresses, given the task in the path. */
export function expandVersionParam(
  param: string | null | undefined,
  taskId: string | null | undefined
): string | null {
  if (!param) return null;
  if (!taskId) return param;
  const match = VERSION_NUMBER.exec(param);
  if (!match) return param;
  // Ids carry no leading zeros, so `v020` has to normalize to match one.
  return `${taskId}-v${Number(match[1])}`;
}

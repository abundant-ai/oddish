// Remembers which agent columns are hidden on an experiment's results table.
//
// The table already keeps `hiddenAgents` in the `?hide=` query param, so a
// refresh — or a link someone shares — restores the columns. What the URL
// cannot do is survive *leaving*: returning from the Experiments list follows a
// plain link with no `?hide=`, and the choice is gone. Storage covers that gap.
//
// The two layers do not compete. `?hide=` still wins whenever it is present, so
// a shared link shows the sender's view; storage is consulted only when the
// param is absent.
//
// Scoped per experiment: agent keys like `codex` recur across experiments, so a
// single global list would hide columns in experiments the user never touched.

// Follows the `oddish.<page>.<setting>` convention set by
// `oddish.tasks.autoRefresh` in tasks-client.tsx.
export const EXPERIMENT_COLUMNS_STORAGE_KEY = "oddish.experiment.hiddenAgents";

// Per-experiment entries would otherwise accumulate for every experiment ever
// opened. Keeping the most recent handful covers the experiments someone is
// actually working in.
export const MAX_REMEMBERED_EXPERIMENTS = 50;

type StoredEntry = { id: string; hidden: string[] };

/** Parse the store, dropping anything that is not a well-formed entry.
 *
 * Every unreadable shape yields an empty store rather than throwing: a corrupt
 * entry must never leave the results table with columns missing and no way to
 * work out why.
 */
function parseStore(raw: string | null): StoredEntry[] {
  if (!raw) return [];

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [];
  }

  if (!Array.isArray(parsed)) return [];

  return parsed.filter((entry): entry is StoredEntry => {
    if (typeof entry !== "object" || entry === null) return false;
    const candidate = entry as { id?: unknown; hidden?: unknown };
    return (
      typeof candidate.id === "string" &&
      Array.isArray(candidate.hidden) &&
      candidate.hidden.every((key) => typeof key === "string")
    );
  });
}

/** The agent keys hidden for one experiment, or `[]` when none are stored. */
export function readHiddenAgents(
  raw: string | null,
  experimentId: string
): string[] {
  const entry = parseStore(raw).find((item) => item.id === experimentId);
  return entry ? [...entry.hidden] : [];
}

/** Record this experiment's hidden columns, returning the new stored value.
 *
 * The experiment moves to the front so the cap evicts by least-recently-set.
 * Hiding nothing removes the entry outright — the default state is not worth
 * a slot, and storing it would evict an experiment the user did customize.
 */
export function writeHiddenAgents(
  raw: string | null,
  experimentId: string,
  hidden: readonly string[]
): string {
  const rest = parseStore(raw).filter((item) => item.id !== experimentId);
  const next =
    hidden.length > 0
      ? [{ id: experimentId, hidden: [...hidden].sort() }, ...rest]
      : rest;

  return JSON.stringify(next.slice(0, MAX_REMEMBERED_EXPERIMENTS));
}

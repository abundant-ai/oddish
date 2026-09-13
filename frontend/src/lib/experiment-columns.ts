/**
 * Remembers which agent columns are hidden on an experiment's results table.
 *
 * The table keeps `hiddenAgents` in the `?hide=` query param, so a refresh —
 * or a shared link — restores the columns. What the URL cannot do is survive
 * leaving: returning from the Experiments list follows a plain link with no
 * `?hide=`, and the choice is gone. Storage covers that gap.
 *
 * The layers do not compete. `?hide=` wins whenever present, so a shared link
 * shows the sender's view; storage is consulted only when the param is absent.
 *
 * Scoped per experiment: agent keys such as `codex` recur across experiments,
 * so a single global list would hide columns in experiments never touched.
 */

/** Storage key, following the `oddish.<page>.<setting>` convention set by `oddish.tasks.autoRefresh`. */
export const EXPERIMENT_COLUMNS_STORAGE_KEY = "oddish.experiment.hiddenAgents";

/**
 * How many experiments to remember, most-recently-set first.
 *
 * Entries would otherwise accumulate for every experiment ever opened; this
 * covers the ones actually being worked in.
 */
export const MAX_REMEMBERED_EXPERIMENTS = 50;

type StoredEntry = { id: string; hidden: string[] };

/**
 * Parse the store, dropping anything that is not a well-formed entry.
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

/**
 * Record this experiment's hidden columns, returning the new stored value.
 *
 * The experiment moves to the front so the cap evicts by least-recently-set.
 * Hiding nothing removes the entry outright — the default state is not worth a
 * slot, and storing it would evict an experiment that was customized.
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

/**
 * The stored hidden columns for one experiment, or `[]` when unavailable.
 *
 * Storage is a convenience, never a dependency: reading it throws outright when
 * the browser blocks it (private mode, an embedded context, cookies disabled),
 * and touching `window.localStorage` at all is enough to raise. A caller that
 * gets `[]` shows every column, which is the same as having saved nothing.
 */
export function loadHiddenAgents(experimentId: string): string[] {
  if (typeof window === "undefined") return [];
  try {
    return readHiddenAgents(
      window.localStorage.getItem(EXPERIMENT_COLUMNS_STORAGE_KEY),
      experimentId
    );
  } catch {
    return [];
  }
}

/**
 * Persist one experiment's hidden columns, discarding the write if storage
 * refuses it.
 *
 * A blocked store raises on read or write, and a full one raises on write with
 * `QuotaExceededError`. Neither is worth failing the render over: the selection
 * already lives in component state and the `?hide=` param, so the table keeps
 * working and only the memory across visits is lost.
 */
export function saveHiddenAgents(
  experimentId: string,
  hidden: readonly string[]
): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(
      EXPERIMENT_COLUMNS_STORAGE_KEY,
      writeHiddenAgents(
        window.localStorage.getItem(EXPERIMENT_COLUMNS_STORAGE_KEY),
        experimentId,
        hidden
      )
    );
  } catch {
    return;
  }
}

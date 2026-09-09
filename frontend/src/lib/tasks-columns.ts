// Which stat fields a TaskCard shows on the Tasks page, and how that choice
// survives a reload.
//
// Unlike the delivery board's filters (see `deliveries.ts`), this is a personal
// display preference, not shareable view state: it stays in browser storage
// rather than the URL so a shared Tasks link never imposes the sender's hidden
// fields on whoever opens it.

export const TASK_COLUMN_KEYS = [
  "lastRun",
  "cost",
  "latestTrials",
  "avgScore",
  "experiments",
] as const;

export type TaskColumnKey = (typeof TASK_COLUMN_KEYS)[number];

export const TASK_COLUMN_LABELS: Record<TaskColumnKey, string> = {
  lastRun: "Last run",
  cost: "Cost",
  latestTrials: "Latest trials",
  avgScore: "Avg score",
  experiments: "Experiments",
};

// Follows the `oddish.<page>.<setting>` convention set by
// `oddish.tasks.autoRefresh` in tasks-client.tsx.
export const TASK_COLUMNS_STORAGE_KEY = "oddish.tasks.columns";

export type TaskColumnVisibility = Record<TaskColumnKey, boolean>;

export function defaultColumnVisibility(): TaskColumnVisibility {
  return Object.fromEntries(
    TASK_COLUMN_KEYS.map((key) => [key, true])
  ) as TaskColumnVisibility;
}

function isTaskColumnKey(value: unknown): value is TaskColumnKey {
  return (
    typeof value === "string" &&
    (TASK_COLUMN_KEYS as readonly string[]).includes(value)
  );
}

/** Read a stored preference, falling back to "everything visible".
 *
 * Storage holds the *hidden* keys rather than the full map so that a field
 * added later shows up by default instead of silently starting hidden for
 * everyone who already has a preference saved.
 *
 * Anything unreadable — absent, malformed JSON, wrong shape, keys that no
 * longer exist — degrades to the default. A corrupt entry must never leave the
 * page with no fields rendered.
 */
export function parseColumnVisibility(
  raw: string | null
): TaskColumnVisibility {
  const visibility = defaultColumnVisibility();
  if (!raw) return visibility;

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return visibility;
  }

  if (!Array.isArray(parsed)) return visibility;

  for (const key of parsed) {
    if (isTaskColumnKey(key)) visibility[key] = false;
  }
  return visibility;
}

/** Serialize to the stored form: a sorted list of hidden keys. */
export function serializeColumnVisibility(
  visibility: TaskColumnVisibility
): string {
  const hidden = TASK_COLUMN_KEYS.filter((key) => !visibility[key]);
  return JSON.stringify(hidden);
}

// The task browser's selection: which tasks are ticked, kept across pages,
// filter changes, reloads, and closed tabs (localStorage, one entry per
// org). Pure helpers here; the React side is app/(app)/tasks/selection-context.

export type SelectionEntry = {
  // Known when the task was ticked from a card; null when it came from
  // "Select all" (ids only).
  cost: number | null;
  estimated: boolean;
};

export type Selection = Map<string, SelectionEntry>;

// Matches the backend's BROWSE_IDS_LIMIT: "Select all" can hand over at most
// this many ids, so the stored selection never grows past it either.
export const SELECTION_LIMIT = 5000;
// DeliveryTasksAdd accepts at most 500 ids per request.
export const DELIVERY_ADD_CHUNK = 500;

const STORAGE_PREFIX = "oddish.tasks.selection.";

export function selectionStorageKey(orgId: string | null | undefined): string {
  return `${STORAGE_PREFIX}${orgId || "personal"}`;
}

type Stored = { v: 1; entries: [string, SelectionEntry][] };

// Tolerates anything: a missing key, an older shape, or hand-edited JSON
// reads as an empty selection rather than a crash on page load.
export function parseStoredSelection(raw: string | null): Selection {
  const out: Selection = new Map();
  if (!raw) return out;
  try {
    const parsed = JSON.parse(raw) as Partial<Stored> | null;
    if (!parsed || parsed.v !== 1 || !Array.isArray(parsed.entries)) return out;
    for (const item of parsed.entries) {
      if (!Array.isArray(item) || typeof item[0] !== "string") continue;
      const entry = item[1] as Partial<SelectionEntry> | undefined;
      out.set(item[0], {
        cost: typeof entry?.cost === "number" ? entry.cost : null,
        estimated: entry?.estimated === true,
      });
      if (out.size >= SELECTION_LIMIT) break;
    }
  } catch {
    return new Map();
  }
  return out;
}

export function serializeSelection(selection: Selection): string {
  const stored: Stored = { v: 1, entries: Array.from(selection.entries()) };
  return JSON.stringify(stored);
}

// Cost total for the sticky bar: only claimable when every ticked task
// carries a cost, i.e. all of them were ticked from a card.
export function selectionTotals(selection: Selection): {
  count: number;
  cost: number | null;
  anyEstimated: boolean;
} {
  let cost = 0;
  let anyEstimated = false;
  let complete = true;
  for (const entry of selection.values()) {
    if (entry.cost === null) complete = false;
    else cost += entry.cost;
    if (entry.estimated) anyEstimated = true;
  }
  return {
    count: selection.size,
    cost: complete && selection.size > 0 ? cost : null,
    anyEstimated,
  };
}

export function chunk<T>(items: T[], size: number): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < items.length; i += size)
    out.push(items.slice(i, i + size));
  return out;
}

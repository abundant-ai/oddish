/**
 * Shared tag color palette + deterministic fallback.
 *
 * Tags created from the dashboard carry an explicit palette color; tags
 * created via the API/CLI may have none, so `tagColor` hashes the key to a
 * stable palette entry — the same tag always renders in the same hue.
 */

export const TAG_COLOR_PALETTE = [
  "#ef4444", // red
  "#f97316", // orange
  "#f59e0b", // amber
  "#22c55e", // green
  "#14b8a6", // teal
  "#3b82f6", // blue
  "#8b5cf6", // violet
  "#ec4899", // pink
] as const;

export function tagColor(key: string, explicit?: string | null): string {
  if (explicit) return explicit;
  let hash = 0;
  for (let i = 0; i < key.length; i += 1) {
    hash = (hash * 31 + key.charCodeAt(i)) >>> 0;
  }
  return TAG_COLOR_PALETTE[hash % TAG_COLOR_PALETTE.length];
}

// Custom colors picked via the color-input slot, newest first. They displace
// the tail of the default palette in the swatch bar so the bar stays one row.
const RECENT_CUSTOM_COLORS_KEY = "oddish:tag-custom-colors";
const RECENT_CUSTOM_COLORS_MAX = 3;

export function getRecentCustomColors(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(RECENT_CUSTOM_COLORS_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((c): c is string => typeof c === "string")
      .slice(0, RECENT_CUSTOM_COLORS_MAX);
  } catch {
    return [];
  }
}

export function rememberCustomColor(color: string): void {
  if (typeof window === "undefined") return;
  const next = [
    color,
    ...getRecentCustomColors().filter((c) => c !== color),
  ].slice(0, RECENT_CUSTOM_COLORS_MAX);
  try {
    window.localStorage.setItem(RECENT_CUSTOM_COLORS_KEY, JSON.stringify(next));
  } catch {
    // localStorage unavailable (private mode) — recents just don't persist.
  }
}

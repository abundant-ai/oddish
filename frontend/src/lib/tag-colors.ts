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

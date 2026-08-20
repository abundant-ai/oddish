import { prepareFileTreeInput } from "@pierre/trees";

/**
 * The path that sorts first among `paths` in @pierre/trees' row order — the
 * top file row of a fully expanded tree, which callers use as their default
 * selection.
 *
 * `prepareFileTreeInput` sorts with the library's own comparator (directories
 * first at each level, then a numeric-aware natural sort — `attempt_2` before
 * `attempt_10`), so the answer cannot drift from what the tree renders.
 */
export function firstFilePath(paths: readonly string[]): string | null {
  return prepareFileTreeInput(paths).paths[0] ?? null;
}

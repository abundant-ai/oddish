import { prepareFileTreeInput } from "@pierre/trees";

/**
 * The path @pierre/trees renders as the top file row — the default selection.
 * Delegates to the library's own sorter (directories first, numeric-aware
 * natural sort) so the answer can't drift from the rendered order.
 */
export function firstFilePath(paths: readonly string[]): string | null {
  return prepareFileTreeInput(paths).paths[0] ?? null;
}

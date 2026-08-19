"use client";

import { useEffect, useRef, type ReactNode } from "react";
import type { FileTree as FileTreeModel } from "@pierre/trees";
import { FileTree, useFileTree } from "@pierre/trees/react";
import { TREES_UNSAFE_CSS } from "@/components/renderers/pierre-options";

interface FileTreePaneProps {
  /**
   * Every file in the tree, as a path. Directory rows are inferred from the
   * path segments, so only leaves belong here.
   *
   * Must be referentially stable (memoize it) — a new array identity rebuilds
   * the tree, which re-expands every directory.
   */
  paths: readonly string[];
  /**
   * The file the rest of the UI is showing. Kept in sync with the tree's own
   * selection: setting it selects and scrolls to that row.
   */
  selectedPath: string | null;
  /** Fired when a *file* row is selected. Directory rows never report. */
  onSelectPath: (path: string) => void;
  /** Rendered above the rows, inside the tree's scroll container. */
  header?: ReactNode;
  className?: string;
}

/**
 * File tree rows, backed by @pierre/trees.
 *
 * The library owns tree shaping, sorting (directories first, then dotfiles,
 * then case-insensitive alpha), expansion, virtualization, type-to-search,
 * and keyboard navigation. Callers own the flat path list and the selection.
 *
 * Like @pierre/diffs, the tree renders into a shadow root, so app styles don't
 * reach it — see `TREES_UNSAFE_CSS` for how it picks up oddish's palette.
 */
export function FileTreePane({
  paths,
  selectedPath,
  onSelectPath,
  header,
  className,
}: FileTreePaneProps) {
  // The tree model is built once (`useFileTree` ignores later option changes),
  // so its selection callback has to reach the current props through refs.
  const onSelectPathRef = useRef(onSelectPath);
  const modelRef = useRef<FileTreeModel | null>(null);
  useEffect(() => {
    onSelectPathRef.current = onSelectPath;
  });

  const { model } = useFileTree({
    density: "compact",
    flattenEmptyDirectories: true,
    initialExpansion: "open",
    paths,
    search: true,
    unsafeCSS: TREES_UNSAFE_CSS,
    onSelectionChange: (selected) => {
      const path = selected.at(-1);
      if (path == null) return;
      // Directory rows are selectable too, but clicking one only toggles
      // expansion — it must not pull the preview off the open file.
      if (modelRef.current?.getItem(path)?.isDirectory() !== false) return;
      onSelectPathRef.current(path);
    },
  });
  modelRef.current = model;

  // `resetPaths` re-applies `initialExpansion`, so a refreshed listing lands
  // fully expanded (the old hand-rolled trees did the same), and keeps the
  // selection whenever the selected path survives the reset.
  const appliedPathsRef = useRef(paths);
  useEffect(() => {
    if (appliedPathsRef.current === paths) return;
    appliedPathsRef.current = paths;
    model.resetPaths(paths);
  }, [model, paths]);

  // Drive the tree from `selectedPath` so deep links and the initial
  // auto-selection highlight the right row. Selecting re-enters
  // `onSelectionChange`, which reports the same path back — a no-op upstream.
  useEffect(() => {
    if (selectedPath == null) return;
    if (model.getSelectedPaths().includes(selectedPath)) return;
    const item = model.getItem(selectedPath);
    if (item == null || item.isDirectory()) return;
    item.select();
    model.scrollToPath(selectedPath, { offset: "nearest" });
  }, [model, paths, selectedPath]);

  return <FileTree className={className} header={header} model={model} />;
}

/**
 * The path that sorts first among `paths` in tree order — the leftmost leaf of
 * a fully expanded tree, which callers use as their default selection.
 *
 * Mirrors @pierre/trees' own row order (directories first, then dot-prefixed
 * names, then case-insensitive alpha) so the answer matches what the tree
 * actually renders at the top.
 */
export function firstFilePath(paths: readonly string[]): string | null {
  let first: string | null = null;
  let firstSegments: string[] = [];
  for (const path of paths) {
    const segments = path.split("/");
    if (first == null || comparePathSegments(segments, firstSegments) < 0) {
      first = path;
      firstSegments = segments;
    }
  }
  return first;
}

function comparePathSegments(left: string[], right: string[]): number {
  const shared = Math.min(left.length, right.length);
  for (let i = 0; i < shared; i += 1) {
    // A segment with more segments after it is a directory, and directories
    // sort above files at the same level.
    const leftIsDir = i < left.length - 1;
    const rightIsDir = i < right.length - 1;
    if (leftIsDir !== rightIsDir) return leftIsDir ? -1 : 1;
    const compared = compareNames(left[i], right[i]);
    if (compared !== 0) return compared;
  }
  return left.length - right.length;
}

function compareNames(left: string, right: string): number {
  const leftIsDot = left.startsWith(".");
  if (leftIsDot !== right.startsWith(".")) return leftIsDot ? -1 : 1;
  return left.toLowerCase().localeCompare(right.toLowerCase());
}

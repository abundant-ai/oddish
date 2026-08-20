"use client";

import { useEffect, useRef } from "react";
import {
  FileTree,
  useFileTree,
  useFileTreeSelection,
} from "@pierre/trees/react";
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
  className?: string;
}

/**
 * File tree rows, backed by @pierre/trees.
 *
 * The library owns tree shaping, sorting, expansion, virtualization,
 * type-to-search, and keyboard navigation. Callers own the flat path list and
 * the selection (`@/lib/file-tree-order` computes the default selection in
 * the same order the tree renders).
 *
 * Like @pierre/diffs, the tree renders into a shadow root, so app styles
 * don't reach it — see `TREES_UNSAFE_CSS` for how it picks up oddish's
 * palette.
 */
export function FileTreePane({
  paths,
  selectedPath,
  onSelectPath,
  className,
}: FileTreePaneProps) {
  // The model is built once — `useFileTree` ignores later option changes —
  // so path updates go through `resetPaths` below.
  const { model } = useFileTree({
    density: "compact",
    flattenEmptyDirectories: true,
    initialExpansion: "open",
    paths,
    search: true,
    unsafeCSS: TREES_UNSAFE_CSS,
  });

  // `resetPaths` re-applies `initialExpansion`, so a refreshed listing lands
  // fully expanded, and it keeps the selection whenever the selected path
  // survives the reset.
  const appliedPathsRef = useRef(paths);
  useEffect(() => {
    if (appliedPathsRef.current === paths) return;
    appliedPathsRef.current = paths;
    model.resetPaths(paths);
  }, [model, paths]);

  // Report file selections. Directory rows are selectable too, but clicking
  // one only toggles expansion — it must not pull the preview off the open
  // file.
  const selected = useFileTreeSelection(model);
  useEffect(() => {
    const path = selected.at(-1);
    if (path == null) return;
    if (model.getItem(path)?.isDirectory() !== false) return;
    onSelectPath(path);
  }, [model, onSelectPath, selected]);

  // Drive the tree from `selectedPath` so deep links and the initial
  // auto-selection highlight the right row. `paths` is a dependency so a
  // deep-linked path that only exists in a later listing gets selected once
  // it arrives. Selecting re-enters the report effect above, which echoes the
  // same path back — a no-op upstream.
  useEffect(() => {
    if (selectedPath == null) return;
    if (model.getSelectedPaths().includes(selectedPath)) return;
    const item = model.getItem(selectedPath);
    if (item == null || item.isDirectory()) return;
    item.select();
    model.scrollToPath(selectedPath, { offset: "nearest" });
  }, [model, paths, selectedPath]);

  return <FileTree className={className} model={model} />;
}

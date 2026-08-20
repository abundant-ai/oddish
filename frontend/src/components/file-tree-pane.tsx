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
   * File paths only — directory rows are inferred. Must be referentially
   * stable: a new array identity rebuilds the tree and re-expands every
   * directory.
   */
  paths: readonly string[];
  /** The open file; setting it selects and scrolls to its row. */
  selectedPath: string | null;
  /** Fired for file rows only — directory rows never report. */
  onSelectPath: (path: string) => void;
  className?: string;
}

/**
 * File tree rows, backed by @pierre/trees. Callers own the path list and the
 * selection; `@/lib/file-tree-order` computes the default selection in tree
 * order. Renders into a shadow root, so theming goes through
 * `TREES_UNSAFE_CSS`, not app styles.
 */
export function FileTreePane({
  paths,
  selectedPath,
  onSelectPath,
  className,
}: FileTreePaneProps) {
  // `useFileTree` builds the model once and ignores later option changes;
  // path updates go through `resetPaths` below.
  const { model } = useFileTree({
    density: "compact",
    flattenEmptyDirectories: true,
    initialExpansion: "open",
    paths,
    search: true,
    unsafeCSS: TREES_UNSAFE_CSS,
  });

  // `resetPaths` re-applies `initialExpansion`, so refreshed listings land
  // fully expanded. It also clears the tree's selection, so the applied-
  // selection marker below resets with it and the open file re-highlights.
  const appliedPathsRef = useRef(paths);
  const appliedSelectionRef = useRef<string | null>(null);
  useEffect(() => {
    if (appliedPathsRef.current === paths) return;
    appliedPathsRef.current = paths;
    appliedSelectionRef.current = null;
    model.resetPaths(paths);
  }, [model, paths]);

  // Directory rows are selectable (clicking toggles expansion) but must not
  // steal the preview, so only file selections report.
  const selected = useFileTreeSelection(model);
  useEffect(() => {
    const path = selected.at(-1);
    if (path == null) return;
    if (model.getItem(path)?.isDirectory() !== false) return;
    onSelectPath(path);
  }, [model, onSelectPath, selected]);

  // Highlight `selectedPath`'s row — but only when the OPEN FILE changes
  // (or first appears in a refreshed listing), never as an echo of the
  // tree's own selection drifting. Re-selecting on drift is what undid a
  // directory collapse: a directory click moves the tree selection off the
  // open file, and an unconditional re-select would re-expand the ancestors
  // the user just collapsed. `paths` stays a dependency so a deep-linked
  // path that only exists in a later listing gets selected when it arrives.
  useEffect(() => {
    if (selectedPath == null) return;
    if (appliedSelectionRef.current === selectedPath) return;
    const item = model.getItem(selectedPath);
    if (item == null || item.isDirectory()) return;
    appliedSelectionRef.current = selectedPath;
    item.select();
    model.scrollToPath(selectedPath, { offset: "nearest" });
  }, [model, paths, selectedPath]);

  return <FileTree className={className} model={model} />;
}

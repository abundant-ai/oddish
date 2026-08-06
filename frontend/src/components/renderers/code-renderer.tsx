"use client";

import { useMemo, useRef } from "react";
import { File as PierreFile } from "@pierre/diffs/react";
import type { SelectedLineRange } from "@pierre/diffs";
import { useIsDark } from "./use-is-dark";
import { PIERRE_THEME, PIERRE_UNSAFE_CSS } from "./pierre-options";
import type { LineRange } from "@/lib/line-range";

interface CodeRendererProps {
  content: string;
  /** Used for language inference; not displayed (the file header is disabled). */
  fileName: string;
  /** Skip language inference and render as plain text (log-free raw views). */
  plainText?: boolean;
  /** Line range to highlight (1-indexed, inclusive) — the ``?lines=`` anchor. */
  selectedLines?: LineRange | null;
  /** Called when the user selects lines (click / shift-click / drag). */
  onSelectLines?: (range: LineRange | null) => void;
}

/** Pierre ranges carry diff-side metadata and can run backwards (drag up). */
function toLineRange(range: SelectedLineRange | null): LineRange | null {
  if (!range) return null;
  return {
    start: Math.min(range.start, range.end),
    end: Math.max(range.start, range.end),
  };
}

/**
 * The one pierre-backed text-file view (shiki highlighting, line numbers,
 * virtualized rendering for large files). Every line-oriented preview —
 * code, logs (shiki has a ``log`` grammar), plain text, and raw mode —
 * renders through this component, so line selection behaves identically
 * everywhere: ``?lines=L12-L20`` deep links highlight and scroll to their
 * range, and selecting lines (click / shift-click / drag) reports back up
 * for URL sync. Language is inferred from the file name unless
 * ``plainText`` is set.
 */
export function CodeRenderer({
  content,
  fileName,
  plainText = false,
  selectedLines,
  onSelectLines,
}: CodeRendererProps) {
  const isDark = useIsDark();
  const containerRef = useRef<HTMLDivElement>(null);

  // Threaded through refs so the pierre options object stays stable across
  // selection changes — rebuilding it would re-initialize the component.
  // Synced during render, not in an effect: pierre's onPostRender can fire
  // before parent effects run, and must see the current values.
  const onSelectLinesRef = useRef(onSelectLines);
  const selectedLinesRef = useRef(selectedLines ?? null);
  onSelectLinesRef.current = onSelectLines;
  selectedLinesRef.current = selectedLines ?? null;

  // Scroll the deep-linked range into view once per file. Pierre renders
  // into a shadow root, so the line element can't be queried from here;
  // instead the target offset is computed from the fixed row height
  // (--diffs-line-height, 1.25rem — the file header is disabled, so rows
  // are the only vertical content).
  const didScrollRef = useRef(false);
  // A new file is a new deep-link surface: the once-only scroll re-arms so
  // a range on the next file scrolls into view too. Keyed on the file name
  // only — a same-file content update (e.g. "Load full file") must not
  // re-arm and yank the reader back to the anchor. A failed first attempt
  // (content not rendered yet) leaves the flag unset, so the empty-to-
  // loaded path needs no content key. Render-time (not an effect) so an
  // early onPostRender never sees the stale armed state.
  const prevFileNameRef = useRef<string | null>(null);
  if (prevFileNameRef.current !== fileName) {
    prevFileNameRef.current = fileName;
    didScrollRef.current = false;
  }
  const scrollToSelection = () => {
    if (didScrollRef.current) return;
    const selection = selectedLinesRef.current;
    const container = containerRef.current;
    if (!selection || !container) return;
    const rootFontSize =
      Number.parseFloat(getComputedStyle(document.documentElement).fontSize) ||
      16;
    const lineHeight = 1.25 * rootFontSize;
    const top = (selection.start - 1) * lineHeight;
    if (container.scrollHeight < top) return; // not fully rendered yet
    didScrollRef.current = true;
    container.scrollTop = Math.max(0, top - container.clientHeight / 3);
  };

  const options = useMemo(
    () => ({
      disableFileHeader: true,
      theme: PIERRE_THEME,
      themeType: (isDark ? "dark" : "light") as "dark" | "light",
      overflow: "scroll" as const,
      unsafeCSS: PIERRE_UNSAFE_CSS,
      enableLineSelection: true,
      onLineSelected: (range: SelectedLineRange | null) => {
        // A user-driven selection means the user is already looking at the
        // lines — disarm the deep-link scroll so it never yanks the view.
        didScrollRef.current = true;
        onSelectLinesRef.current?.(toLineRange(range));
      },
      onPostRender: () => {
        scrollToSelection();
      },
    }),
    [isDark]
  );

  return (
    <div ref={containerRef} className="h-full overflow-auto">
      <PierreFile
        file={{
          name: fileName,
          contents: content,
          ...(plainText ? { lang: "text" as const } : null),
        }}
        options={options}
        selectedLines={selectedLines ?? null}
      />
    </div>
  );
}

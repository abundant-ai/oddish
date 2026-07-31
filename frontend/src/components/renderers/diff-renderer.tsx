"use client";

import { useMemo } from "react";
import { PatchDiff } from "@pierre/diffs/react";
import { CodeRenderer } from "./code-renderer";
import { useIsDark } from "./use-is-dark";
import { PIERRE_THEME, PIERRE_UNSAFE_CSS } from "./pierre-options";

interface DiffRendererProps {
  content: string;
  fileName: string;
}

/** Loose check that the content is actually a unified diff / git patch. */
function looksLikeUnifiedDiff(content: string): boolean {
  return (
    /^@@ -\d/m.test(content) ||
    /^diff --git /m.test(content) ||
    (/^--- /m.test(content) && /^\+\+\+ /m.test(content))
  );
}

/**
 * Renders `.diff` / `.patch` files with @pierre/diffs — parsed hunks,
 * word-level line diffs, per-file headers for multi-file patches. Files
 * that aren't parseable as a unified diff fall back to the plain code view.
 */
export function DiffRenderer({ content, fileName }: DiffRendererProps) {
  const isDark = useIsDark();

  const options = useMemo(
    () => ({
      theme: PIERRE_THEME,
      themeType: (isDark ? "dark" : "light") as "dark" | "light",
      diffStyle: "unified" as const,
      diffIndicators: "bars" as const,
      lineDiffType: "word" as const,
      overflow: "scroll" as const,
      unsafeCSS: PIERRE_UNSAFE_CSS,
    }),
    [isDark]
  );

  if (!looksLikeUnifiedDiff(content)) {
    return <CodeRenderer content={content} fileName={fileName} />;
  }

  return (
    <div className="h-full overflow-auto p-2">
      <PatchDiff patch={content} options={options} />
    </div>
  );
}

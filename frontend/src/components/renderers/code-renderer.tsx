"use client";

import { useMemo } from "react";
import { File as PierreFile } from "@pierre/diffs/react";
import { useIsDark } from "./use-is-dark";
import {
  PIERRE_THEME,
  PIERRE_UNSAFE_CSS,
  PIERRE_STYLE_OVERRIDES,
} from "./pierre-options";

interface CodeRendererProps {
  content: string;
  /** Used for language inference; not displayed (the file header is disabled). */
  fileName: string;
}

/**
 * Full-file code view powered by @pierre/diffs (shiki highlighting, line
 * numbers, virtualized rendering for large files). Language is inferred
 * from the file name.
 */
export function CodeRenderer({ content, fileName }: CodeRendererProps) {
  const isDark = useIsDark();

  const options = useMemo(
    () => ({
      disableFileHeader: true,
      theme: PIERRE_THEME,
      themeType: (isDark ? "dark" : "light") as "dark" | "light",
      overflow: "scroll" as const,
      unsafeCSS: PIERRE_UNSAFE_CSS,
    }),
    [isDark]
  );

  const style = useMemo(() => PIERRE_STYLE_OVERRIDES(isDark), [isDark]);

  return (
    <div className="h-full overflow-auto">
      <PierreFile
        file={{ name: fileName, contents: content }}
        options={options}
        style={style}
      />
    </div>
  );
}

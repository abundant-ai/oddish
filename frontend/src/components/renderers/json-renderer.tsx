"use client";

import { useMemo } from "react";
import { CodeRenderer } from "./code-renderer";

interface JsonRendererProps {
  content: string;
}

/**
 * Pretty-printed JSON with syntax highlighting and line numbers. Invalid
 * JSON is shown as-is (still highlighted) rather than erroring out.
 */
export function JsonRenderer({ content }: JsonRendererProps) {
  const pretty = useMemo(() => {
    try {
      return JSON.stringify(JSON.parse(content), null, 2);
    } catch {
      return content;
    }
  }, [content]);

  return <CodeRenderer content={pretty} fileName="data.json" />;
}

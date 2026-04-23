"use client";

import { useMemo } from "react";

interface LogRendererProps {
  content: string;
}

export function LogRenderer({ content }: LogRendererProps) {
  const lines = useMemo(() => content.split("\n"), [content]);

  const getLineStyle = (line: string) => {
    const lower = line.toLowerCase();
    if (lower.includes("error") || lower.includes("fail")) {
      return "text-red-600 dark:text-red-400";
    }
    if (lower.includes("warn")) {
      return "text-yellow-600 dark:text-yellow-400";
    }
    if (lower.includes("info")) {
      return "text-blue-600 dark:text-blue-400";
    }
    if (lower.includes("success") || lower.includes("pass")) {
      return "text-emerald-600 dark:text-emerald-400";
    }
    return "text-foreground";
  };

  return (
    <div className="space-y-0.5 overflow-auto p-3 font-mono text-xs">
      {lines.map((line, i) => (
        <div key={i} className={getLineStyle(line)}>
          <span className="mr-3 select-none text-muted-foreground/60">
            {i + 1}
          </span>
          {line || " "}
        </div>
      ))}
    </div>
  );
}

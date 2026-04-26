"use client";

import { useState } from "react";
import type { JSX } from "react";
import { Button } from "@/components/ui/button";

interface JsonRendererProps {
  content: string;
}

export function JsonRenderer({ content }: JsonRendererProps) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  let parsed: unknown;
  try {
    parsed = JSON.parse(content);
  } catch {
    return <div className="p-4 text-destructive">Invalid JSON</div>;
  }

  const toggleExpand = (path: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
      }
      return next;
    });
  };

  const renderValue = (value: unknown, path = "", depth = 0): JSX.Element => {
    if (value === null) {
      return <span className="text-muted-foreground">null</span>;
    }

    if (typeof value === "boolean") {
      return (
        <span className="text-blue-600 dark:text-blue-400">
          {value.toString()}
        </span>
      );
    }

    if (typeof value === "number") {
      return (
        <span className="text-emerald-600 dark:text-emerald-400">{value}</span>
      );
    }

    if (typeof value === "string") {
      return (
        <span className="text-amber-600 dark:text-amber-400">
          &quot;{value}&quot;
        </span>
      );
    }

    if (Array.isArray(value)) {
      const isExpanded = expanded.has(path);
      return (
        <div>
          <Button
            type="button"
            variant="ghost"
            onClick={() => toggleExpand(path)}
            className="h-auto justify-start bg-transparent p-0 font-mono text-xs font-normal text-muted-foreground hover:bg-transparent hover:text-foreground"
          >
            {isExpanded ? "▼" : "▶"} Array[{value.length}]
          </Button>
          {isExpanded && (
            <div className="ml-2 border-l border-border pl-4">
              {value.map((item, i) => (
                <div key={i} className="py-1">
                  <span className="text-muted-foreground">{i}: </span>
                  {renderValue(item, `${path}[${i}]`, depth + 1)}
                </div>
              ))}
            </div>
          )}
        </div>
      );
    }

    if (typeof value === "object") {
      const obj = value as Record<string, unknown>;
      const keys = Object.keys(obj);
      const isExpanded = expanded.has(path);
      return (
        <div>
          <Button
            type="button"
            variant="ghost"
            onClick={() => toggleExpand(path)}
            className="h-auto justify-start bg-transparent p-0 font-mono text-xs font-normal text-muted-foreground hover:bg-transparent hover:text-foreground"
          >
            {isExpanded ? "▼" : "▶"} Object{"{"}
            {keys.length}
            {"}"}
          </Button>
          {isExpanded && (
            <div className="ml-2 border-l border-border pl-4">
              {keys.map((key) => (
                <div key={key} className="py-1">
                  <span className="text-sky-600 dark:text-sky-400">{key}</span>
                  <span className="text-muted-foreground">: </span>
                  {renderValue(obj[key], `${path}.${key}`, depth + 1)}
                </div>
              ))}
            </div>
          )}
        </div>
      );
    }

    return <span>{String(value)}</span>;
  };

  return (
    <div className="overflow-auto p-3 font-mono text-xs">
      {renderValue(parsed)}
    </div>
  );
}

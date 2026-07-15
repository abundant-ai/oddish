"use client";

import { useState, type ReactNode } from "react";
import { ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { CodeBlock } from "@/components/code-block";
import { cn } from "@/lib/utils";

const LONG_ARGS_CHARS = 100;

const SOURCE_COLORS: Record<string, string> = {
  system: "text-gray-500",
  user: "text-blue-500",
  agent: "text-purple-500",
};

export function StepHeader({
  index,
  source,
  model,
  preview,
  badges,
}: {
  index: number | string;
  source: string;
  model?: string | null;
  preview?: string | null;
  badges?: ReactNode;
}) {
  const label = source === "agent" ? "Agent" : source;
  return (
    <div className="flex min-w-0 flex-1 items-center gap-3 overflow-hidden pr-2">
      <div className="flex shrink-0 items-center gap-2">
        <span className="text-muted-foreground font-mono text-xs">
          #{index}
        </span>
        <span
          className={cn(
            "text-xs font-medium capitalize",
            SOURCE_COLORS[source] || "text-gray-500"
          )}
        >
          {label}
        </span>
        {model && (
          <span className="text-muted-foreground text-xs">{model}</span>
        )}
      </div>

      <span className="text-muted-foreground min-w-0 flex-1 truncate text-xs">
        {preview || <span className="italic">No message</span>}
      </span>

      {badges && (
        <div className="flex shrink-0 items-center gap-1.5">{badges}</div>
      )}
    </div>
  );
}

export function ToolCallBlock({
  name,
  args,
  trailing,
}: {
  name: string;
  args: string;
  trailing?: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const hasArgs = args.trim().length > 0;
  const isLong = hasArgs && args.length > LONG_ARGS_CHARS;
  const showArgs = hasArgs && (open || !isLong);

  return (
    <div className="overflow-hidden rounded border border-purple-500/20">
      <Button
        type="button"
        variant="ghost"
        size="sm"
        onClick={() => setOpen((v) => !v)}
        className="w-full justify-start gap-2 bg-purple-500/10 px-2 py-1.5 text-left hover:bg-purple-500/15"
      >
        <ChevronRight
          className={cn(
            "h-3 w-3 shrink-0 text-purple-500 transition-transform",
            open && "rotate-90"
          )}
        />
        <span className="font-mono text-xs text-purple-500">
          {name || "tool"}
        </span>
        {isLong && !open && (
          <span className="text-muted-foreground text-[10px]">
            (click to expand)
          </span>
        )}
        {trailing}
      </Button>
      {showArgs && (
        <CodeBlock code={args} language="json" className="rounded-none" />
      )}
    </div>
  );
}

export function ObservationBlock({
  content,
  isError,
  trailing,
}: {
  content: string;
  isError?: boolean;
  trailing?: ReactNode;
}) {
  return (
    <div
      className={cn(
        "space-y-1",
        isError && "border-l-2 border-red-500/60 pl-2"
      )}
    >
      <CodeBlock code={content} language="bash" />
      {(isError || trailing) && (
        <div className="flex items-center gap-1.5 pl-0.5 text-[10px]">
          {isError && <span className="font-medium text-red-500">error</span>}
          {trailing}
        </div>
      )}
    </div>
  );
}

"use client";

import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { formatMs } from "@/lib/utils";

function formatTimestamp(value: string): string {
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function formatDateShort(value: string): string {
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

interface TimingBreakdownBarProps {
  createdAt: string | null | undefined;
  startedAt: string | null | undefined;
  finishedAt: string | null | undefined;
  /** Compact mode: no header, integrates timestamps inline. */
  compact?: boolean;
}

export function TimingBreakdownBar({
  createdAt,
  startedAt,
  finishedAt,
  compact = false,
}: TimingBreakdownBarProps) {
  if (!createdAt || !startedAt || !finishedAt) return null;

  const created = new Date(createdAt).getTime();
  const started = new Date(startedAt).getTime();
  const finished = new Date(finishedAt).getTime();

  if (Number.isNaN(created) || Number.isNaN(started) || Number.isNaN(finished))
    return null;

  const queueMs = Math.max(0, started - created);
  const execMs = Math.max(0, finished - started);
  const totalMs = queueMs + execMs;

  if (totalMs === 0) return null;

  const segments = [
    {
      key: "queue",
      value: queueMs,
      color: "bg-slate-500",
      label: "Queue",
    },
    {
      key: "execution",
      value: execMs,
      color: "bg-blue-500",
      label: "Execution",
    },
  ].filter((s) => s.value > 0);

  const minWidthPercent = 8;
  const widths = segments.map((s) => {
    const raw = (s.value / totalMs) * 100;
    return Math.max(raw, minWidthPercent);
  });

  if (compact) {
    return (
      <TooltipProvider>
        <div>
          <div className="relative">
            <div className="flex h-2.5 gap-0.5 overflow-hidden rounded-full">
              {segments.map((segment, idx) => (
                <Tooltip key={segment.key}>
                  <TooltipTrigger asChild>
                    <div
                      className={`${segment.color} cursor-default`}
                      style={{
                        width: `${widths[idx]}%`,
                      }}
                    />
                  </TooltipTrigger>
                  <TooltipContent>
                    {segment.label}: {formatMs(segment.value)}
                  </TooltipContent>
                </Tooltip>
              ))}
            </div>
          </div>
          <div className="mt-1.5 flex items-center justify-between text-[10px] text-muted-foreground">
            <div className="flex items-center gap-2.5">
              {segments.map((segment) => (
                <span key={segment.key} className="flex items-center gap-1">
                  <span
                    className={`inline-block h-1.5 w-1.5 rounded-full ${segment.color}`}
                  />
                  {segment.label}: {formatMs(segment.value)}
                </span>
              ))}
            </div>
            <span className="font-mono tabular-nums">
              {formatDateShort(createdAt)} {formatTimestamp(createdAt)} →{" "}
              {formatTimestamp(finishedAt)}
            </span>
          </div>
        </div>
      </TooltipProvider>
    );
  }

  return (
    <TooltipProvider>
      <div>
        <div className="mb-1.5 flex items-center gap-2">
          <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
            Timing
          </span>
          <span className="text-xs text-muted-foreground">
            {formatMs(totalMs)} total
          </span>
        </div>
        <div className="relative">
          <div className="flex h-3 gap-0.5 overflow-hidden rounded-full">
            {segments.map((segment, idx) => (
              <Tooltip key={segment.key}>
                <TooltipTrigger asChild>
                  <div
                    className={`${segment.color} cursor-default`}
                    style={{
                      width: `${widths[idx]}%`,
                    }}
                  />
                </TooltipTrigger>
                <TooltipContent>
                  {segment.label}: {formatMs(segment.value)}
                </TooltipContent>
              </Tooltip>
            ))}
          </div>
        </div>
        <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1.5">
          {segments.map((segment) => (
            <div
              key={segment.key}
              className="flex items-center gap-1 text-[10px]"
            >
              <div className={`h-2 w-2 rounded-full ${segment.color}`} />
              <span className="text-muted-foreground">
                {segment.label}: {formatMs(segment.value)}
              </span>
            </div>
          ))}
        </div>
      </div>
    </TooltipProvider>
  );
}

"use client";

import { useEffect, useState } from "react";
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
  // Live ticker: when finishedAt is missing, the queue/exec segment(s)
  // grow against "now". We tick once a second so the bar feels live in
  // between SWR refreshes from the parent.
  const isLive = Boolean(createdAt) && !finishedAt;
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!isLive) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [isLive]);

  if (!createdAt) return null;

  const created = new Date(createdAt).getTime();
  if (Number.isNaN(created)) return null;

  const startedRaw = startedAt ? new Date(startedAt).getTime() : null;
  const started =
    startedRaw != null && !Number.isNaN(startedRaw) ? startedRaw : null;

  const finishedRaw = finishedAt ? new Date(finishedAt).getTime() : null;
  const finished =
    finishedRaw != null && !Number.isNaN(finishedRaw) ? finishedRaw : null;

  // Determine effective end of each segment:
  // - finished: queue = created→started, exec = started→finished
  // - running: queue = created→started, exec = started→now
  // - queued/pending: queue = created→now (no exec yet)
  const effectiveQueueEnd = started ?? finished ?? now;
  const queueMs = Math.max(0, effectiveQueueEnd - created);

  const execStart = started;
  const execEnd = execStart != null ? (finished ?? now) : null;
  const execMs =
    execStart != null && execEnd != null ? Math.max(0, execEnd - execStart) : 0;

  const totalMs = queueMs + execMs;
  if (totalMs === 0) return null;

  type Segment = {
    key: string;
    value: number;
    color: string;
    label: string;
    live: boolean;
  };

  const queueIsLive = isLive && started == null;
  const execIsLive = isLive && started != null;

  const segments: Segment[] = [
    {
      key: "queue",
      value: queueMs,
      color: "bg-slate-500",
      label: "Queue",
      live: queueIsLive,
    },
    {
      key: "execution",
      value: execMs,
      color: "bg-blue-500",
      label: "Execution",
      live: execIsLive,
    },
  ].filter((s) => s.value > 0 || s.live);

  const minWidthPercent = 8;
  const widths = segments.map((s) => {
    const raw = totalMs > 0 ? (s.value / totalMs) * 100 : 0;
    return Math.max(raw, minWidthPercent);
  });

  const liveBadge = " (live)";
  const endTimestamp =
    finishedAt != null
      ? formatTimestamp(finishedAt)
      : startedAt != null
        ? `${formatTimestamp(startedAt)} → now`
        : "now";

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
                      className={`${segment.color} cursor-default ${
                        segment.live ? "animate-pulse" : ""
                      }`}
                      style={{
                        width: `${widths[idx]}%`,
                      }}
                    />
                  </TooltipTrigger>
                  <TooltipContent>
                    {segment.label}: {formatMs(segment.value)}
                    {segment.live ? liveBadge : ""}
                  </TooltipContent>
                </Tooltip>
              ))}
            </div>
          </div>
          <div className="text-muted-foreground mt-1.5 flex items-center justify-between text-[10px]">
            <div className="flex items-center gap-2.5">
              {segments.map((segment) => (
                <span key={segment.key} className="flex items-center gap-1">
                  <span
                    className={`inline-block h-1.5 w-1.5 rounded-full ${
                      segment.color
                    } ${segment.live ? "animate-pulse" : ""}`}
                  />
                  {segment.label}: {formatMs(segment.value)}
                  {segment.live ? liveBadge : ""}
                </span>
              ))}
            </div>
            <span className="font-mono tabular-nums">
              {formatDateShort(createdAt)} {formatTimestamp(createdAt)} →{" "}
              {endTimestamp}
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
          <span className="text-muted-foreground text-[10px] tracking-wider uppercase">
            Timing
          </span>
          <span className="text-muted-foreground text-xs">
            {formatMs(totalMs)} total{isLive ? liveBadge : ""}
          </span>
        </div>
        <div className="relative">
          <div className="flex h-3 gap-0.5 overflow-hidden rounded-full">
            {segments.map((segment, idx) => (
              <Tooltip key={segment.key}>
                <TooltipTrigger asChild>
                  <div
                    className={`${segment.color} cursor-default ${
                      segment.live ? "animate-pulse" : ""
                    }`}
                    style={{
                      width: `${widths[idx]}%`,
                    }}
                  />
                </TooltipTrigger>
                <TooltipContent>
                  {segment.label}: {formatMs(segment.value)}
                  {segment.live ? liveBadge : ""}
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
              <div
                className={`h-2 w-2 rounded-full ${segment.color} ${
                  segment.live ? "animate-pulse" : ""
                }`}
              />
              <span className="text-muted-foreground">
                {segment.label}: {formatMs(segment.value)}
                {segment.live ? liveBadge : ""}
              </span>
            </div>
          ))}
        </div>
      </div>
    </TooltipProvider>
  );
}

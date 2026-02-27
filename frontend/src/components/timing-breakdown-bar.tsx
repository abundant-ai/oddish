"use client";

import { useState } from "react";

function formatMs(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  if (minutes < 60) return `${minutes}m ${remainingSeconds}s`;
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  return `${hours}h ${remainingMinutes}m`;
}

interface TimingBreakdownBarProps {
  createdAt: string | null | undefined;
  startedAt: string | null | undefined;
  finishedAt: string | null | undefined;
}

export function TimingBreakdownBar({
  createdAt,
  startedAt,
  finishedAt,
}: TimingBreakdownBarProps) {
  const [hoveredSegment, setHoveredSegment] = useState<string | null>(null);

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

  return (
    <div>
      <div className="flex items-center gap-2 mb-1.5">
        <span className="text-[10px] text-muted-foreground uppercase tracking-wider">
          Timing
        </span>
        <span className="text-xs text-muted-foreground">
          {formatMs(totalMs)} total
        </span>
      </div>
      <div className="relative">
        {hoveredSegment && (
          <div className="absolute bottom-full mb-1 left-1/2 -translate-x-1/2 z-10 pointer-events-none">
            <div className="bg-popover border border-border rounded px-2 py-1 text-xs whitespace-nowrap shadow-md">
              {segments.find((s) => s.key === hoveredSegment)?.label}:{" "}
              {formatMs(
                segments.find((s) => s.key === hoveredSegment)?.value ?? 0,
              )}
            </div>
          </div>
        )}
        <div className="flex h-3 overflow-hidden rounded-full gap-0.5">
          {segments.map((segment, idx) => (
            <div
              key={segment.key}
              className={`${segment.color} transition-opacity cursor-default`}
              style={{
                width: `${widths[idx]}%`,
                opacity:
                  hoveredSegment && hoveredSegment !== segment.key ? 0.3 : 1,
              }}
              onMouseEnter={() => setHoveredSegment(segment.key)}
              onMouseLeave={() => setHoveredSegment(null)}
            />
          ))}
        </div>
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-1.5 mt-1.5">
        {segments.map((segment) => (
          <div
            key={segment.key}
            className="flex items-center gap-1 text-[10px]"
          >
            <div className={`w-2 h-2 rounded-full ${segment.color}`} />
            <span className="text-muted-foreground">
              {segment.label}: {formatMs(segment.value)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

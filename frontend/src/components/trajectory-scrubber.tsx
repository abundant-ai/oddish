"use client";

import { useCallback, useMemo, useRef, useState } from "react";
import { Star, TriangleAlert } from "lucide-react";
import { formatMs } from "@/lib/utils";
import type { TrajectoryStep } from "@/lib/types";
import {
  segmentWidthsPct,
  stepDurationsMs,
  stepTokens,
} from "@/lib/trajectory-metrics";
import type { Segment } from "@/lib/trajectory-segments";

interface TrajectoryScrubberProps {
  steps: TrajectoryStep[];
  /** Per-step failure sniff, index-aligned with `steps` (⚠ ticks). */
  errorFlags: boolean[];
  /** step_id → owning segment (see `segmentOwners`); empty without a summary. */
  ownerByStep: ReadonlyMap<number, Segment>;
  /** segment key → CSS color (semantic taxonomy colors / ordered slots). */
  colorFor: ReadonlyMap<string, string>;
  /** step_id → key-step title/why from the summary (★ markers + hover). */
  highlights: ReadonlyMap<number, { title: string; why: string }>;
  /** Index of the step currently at the top of the viewport (the playhead). */
  activeIndex: number | null;
  /**
   * Step indices matching the active keyword filter; while set, the track is
   * dimmed and only matching steps stay lit so it agrees with the filtered
   * list. Null when no filter is active.
   */
  matchedIndices?: ReadonlySet<number> | null;
  onStepSelect: (index: number) => void;
}

function stepPreview(step: TrajectoryStep): string | null {
  if (step.tool_calls?.length) {
    const names = step.tool_calls
      .map((c) => c.function_name)
      .filter(Boolean)
      .join(", ");
    if (names) return names;
  }
  const msg = step.message;
  if (typeof msg === "string" && msg.trim()) return msg.trim();
  if (Array.isArray(msg)) {
    const text = msg
      .filter((p) => p.type === "text")
      .map((p) => p.text ?? "")
      .join(" ")
      .trim();
    if (text) return text;
  }
  return null;
}

interface TrackRun {
  startIdx: number;
  endIdx: number;
  key: string | null;
}

/**
 * YouTube-scrubber-style overview of a trajectory: a component-colored,
 * duration-proportional track with ★ (key step) and ⚠ (failed-looking
 * output) markers. Hover previews a step, click jumps to it, and the
 * playhead tracks the step being read.
 *
 * Interaction is continuous (pointer position → step via the duration
 * scale), not one DOM element per step, so it stays usable and cheap at
 * hundreds of steps: the track renders one span per contiguous component
 * run and never overflows its container.
 */
export function TrajectoryScrubber({
  steps,
  errorFlags,
  ownerByStep,
  colorFor,
  highlights,
  activeIndex,
  matchedIndices = null,
  onStepSelect,
}: TrajectoryScrubberProps) {
  // Step index under the pointer (or moved to via arrow keys).
  const [cursor, setCursor] = useState<number | null>(null);
  const surfaceRef = useRef<HTMLDivElement | null>(null);

  const durations = useMemo(() => stepDurationsMs(steps), [steps]);
  // The per-step visibility floor shrinks as counts grow — precision comes
  // from the continuous pointer mapping, not from per-step minimum widths.
  const widths = useMemo(
    () =>
      segmentWidthsPct(
        durations,
        Math.min(1.25, 50 / Math.max(1, durations.length)),
      ),
    [durations],
  );
  // edges[i] = left x of segment i (percent); edges[n] = 100.
  const edges = useMemo(() => {
    const out = [0];
    let x = 0;
    for (const w of widths) {
      x += w;
      out.push(x);
    }
    return out;
  }, [widths]);
  const centers = useMemo(
    () => widths.map((w, i) => edges[i] + w / 2),
    [widths, edges],
  );

  // Contiguous same-component runs — the track draws one span per run, so
  // its element count scales with the number of components, not steps.
  const runs = useMemo(() => {
    const out: TrackRun[] = [];
    steps.forEach((s, i) => {
      const key = ownerByStep.get(Number(s.step_id))?.key ?? null;
      const last = out[out.length - 1];
      if (last && last.key === key) last.endIdx = i;
      else out.push({ startIdx: i, endIdx: i, key });
    });
    return out;
  }, [steps, ownerByStep]);

  const indexAtPct = useCallback(
    (pct: number) => {
      const clamped = Math.min(99.999, Math.max(0, pct));
      let lo = 0;
      let hi = widths.length - 1;
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (edges[mid + 1] <= clamped) lo = mid + 1;
        else hi = mid;
      }
      return lo;
    },
    [edges, widths.length],
  );

  const pctFromEvent = useCallback(
    (e: React.PointerEvent | React.MouseEvent) => {
      const rect = surfaceRef.current?.getBoundingClientRect();
      if (!rect || rect.width === 0) return 0;
      return ((e.clientX - rect.left) / rect.width) * 100;
    },
    [],
  );

  if (steps.length === 0) return null;

  const hasSegments = ownerByStep.size > 0;
  const filterActive = matchedIndices != null;
  const cursorStep = cursor != null ? steps[cursor] : null;
  const cursorSegment = cursorStep
    ? (ownerByStep.get(Number(cursorStep.step_id)) ?? null)
    : null;
  const cursorHighlight = cursorStep
    ? (highlights.get(Number(cursorStep.step_id)) ?? null)
    : null;
  // Keep the tooltip bubble inside the bar regardless of position.
  const tooltipLeft =
    cursor != null ? Math.min(86, Math.max(14, centers[cursor])) : 0;

  const colorForKey = (key: string | null) =>
    hasSegments
      ? key
        ? (colorFor.get(key) ?? "var(--phase-other)")
        : "var(--phase-other)"
      : "var(--muted-foreground)";

  // Thin ⚠ ticks by position so error-dense runs don't render an unreadable
  // wall of overlapping triangles; ★ markers are few and always render.
  const visibleErrorTicks = new Set<number>();
  {
    let lastX = -Infinity;
    steps.forEach((step, i) => {
      if (!errorFlags[i] || highlights.has(Number(step.step_id))) return;
      if (centers[i] - lastX >= 1.2) {
        visibleErrorTicks.add(i);
        lastX = centers[i];
      }
    });
  }

  const markers = steps
    .map((step, i) => {
      const isHighlight = highlights.has(Number(step.step_id));
      if (!isHighlight && !visibleErrorTicks.has(i)) return null;
      const dim = filterActive && !matchedIndices.has(i);
      return (
        <span
          key={step.step_id}
          className="pointer-events-none absolute -translate-x-1/2"
          style={{ left: `${centers[i]}%`, opacity: dim ? 0.25 : 1 }}
        >
          {isHighlight ? (
            <Star className="h-2.5 w-2.5 fill-amber-400 text-amber-500" />
          ) : (
            <TriangleAlert className="h-2.5 w-2.5 text-red-500" />
          )}
        </span>
      );
    })
    .filter(Boolean);

  return (
    <div className="relative select-none">
      {/* Hover preview bubble. Rendered below the track — the scrubber sits
          sticky at the top of the pane, so above would leave the viewport. */}
      {cursorStep && (
        <div
          className="border-border bg-popover text-popover-foreground pointer-events-none absolute top-full z-20 mt-1.5 w-56 -translate-x-1/2 rounded-md border p-2 text-left shadow-md"
          style={{ left: `${tooltipLeft}%` }}
          role="presentation"
        >
          <div className="flex items-center gap-1.5 text-xs font-medium">
            <span className="font-mono">Step {cursorStep.step_id}</span>
            {cursorSegment && (
              <span className="text-muted-foreground flex min-w-0 items-center gap-1 truncate">
                <span
                  className="h-2 w-2 shrink-0 rounded-sm"
                  style={{ background: colorForKey(cursorSegment.key) }}
                />
                <span className="truncate">{cursorSegment.label}</span>
              </span>
            )}
          </div>
          {cursor != null && (
            <div className="text-muted-foreground mt-0.5 flex flex-wrap gap-x-2 font-mono text-[10px]">
              <span>+{formatMs(durations[cursor])}</span>
              {stepTokens(cursorStep) != null && (
                <span>
                  {(stepTokens(cursorStep) ?? 0).toLocaleString()} tok
                </span>
              )}
              {errorFlags[cursor] && (
                <span className="text-red-500">error output</span>
              )}
            </div>
          )}
          {/* Key steps get their summary title + why; ordinary steps fall
              back to a raw preview (tool names / first message line). */}
          {cursorHighlight ? (
            <div className="mt-1 text-[11px] leading-snug">
              <div className="flex items-start gap-1">
                <Star className="mt-0.5 h-3 w-3 shrink-0 fill-amber-400 text-amber-500" />
                <span className="font-medium">{cursorHighlight.title}</span>
              </div>
              {cursorHighlight.why && (
                <p className="text-muted-foreground mt-0.5 line-clamp-3 [overflow-wrap:anywhere]">
                  {cursorHighlight.why}
                </p>
              )}
            </div>
          ) : (
            stepPreview(cursorStep) && (
              <p className="text-muted-foreground mt-1 line-clamp-2 text-[11px] leading-snug [overflow-wrap:anywhere]">
                {stepPreview(cursorStep)}
              </p>
            )
          )}
        </div>
      )}

      {/* Interactive surface: track + markers share one continuous
          pointer/keyboard mapping (slider semantics). */}
      <div
        ref={surfaceRef}
        role="slider"
        tabIndex={0}
        aria-label={`Trajectory scrubber, ${steps.length} steps`}
        aria-valuemin={0}
        aria-valuemax={steps.length - 1}
        aria-valuenow={cursor ?? activeIndex ?? 0}
        aria-valuetext={`Step ${steps[cursor ?? activeIndex ?? 0]?.step_id}`}
        className="focus-visible:outline-ring cursor-pointer pt-1 focus-visible:outline focus-visible:outline-1"
        onPointerMove={(e) => setCursor(indexAtPct(pctFromEvent(e)))}
        onPointerLeave={() => setCursor(null)}
        onClick={(e) => onStepSelect(indexAtPct(pctFromEvent(e)))}
        onKeyDown={(e) => {
          if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
            e.preventDefault();
            const delta = e.key === "ArrowLeft" ? -1 : 1;
            setCursor((prev) => {
              const base = prev ?? activeIndex ?? 0;
              return Math.min(steps.length - 1, Math.max(0, base + delta));
            });
          } else if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            if (cursor != null) onStepSelect(cursor);
          }
        }}
      >
        {/* Component-colored track: one span per contiguous run. */}
        <div className="relative flex h-3 items-stretch overflow-hidden rounded-sm">
          {runs.map((run) => (
            <div
              key={run.startIdx}
              className="border-background h-full border-r last:border-r-0"
              style={{
                width: `${edges[run.endIdx + 1] - edges[run.startIdx]}%`,
                background: colorForKey(run.key),
                opacity: filterActive ? 0.15 : hasSegments ? 1 : 0.35,
              }}
            />
          ))}
          {/* Matching steps stay lit while a keyword filter is active. */}
          {filterActive &&
            [...matchedIndices].map((i) =>
              steps[i] ? (
                <div
                  key={i}
                  className="absolute inset-y-0"
                  style={{
                    left: `${edges[i]}%`,
                    width: `${Math.max(widths[i], 0.4)}%`,
                    background: colorForKey(
                      ownerByStep.get(Number(steps[i].step_id))?.key ?? null,
                    ),
                  }}
                />
              ) : null,
            )}
        </div>

        {/* Event markers (★ key steps, ⚠ failed-looking output) */}
        <div className="relative mt-0.5 h-3">{markers}</div>
      </div>

      {/* Cursor line (hover / keyboard position) */}
      {cursor != null && (
        <div
          className="bg-foreground/40 pointer-events-none absolute top-0 bottom-3 z-10 w-px -translate-x-1/2"
          style={{ left: `${centers[cursor]}%` }}
        />
      )}

      {/* Playhead */}
      {activeIndex != null && centers[activeIndex] != null && (
        <div
          className="pointer-events-none absolute top-0 bottom-3 z-10 flex -translate-x-1/2 flex-col items-center transition-[left] duration-150 ease-out"
          style={{ left: `${centers[activeIndex]}%` }}
        >
          <div className="border-t-foreground/70 h-0 w-0 border-x-4 border-t-[5px] border-x-transparent" />
          <div className="bg-foreground/70 w-px flex-1" />
        </div>
      )}
    </div>
  );
}

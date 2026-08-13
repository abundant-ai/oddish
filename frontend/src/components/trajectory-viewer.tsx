"use client";

import { useState, useRef, useEffect, useMemo, useCallback } from "react";
import useSWR from "swr";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Route, Download, ImageOff, Search, X } from "lucide-react";
import {
  StepHeader,
  ToolCallBlock,
  ObservationBlock,
} from "@/components/trajectory-blocks";
import { TrajectorySummary } from "@/components/trajectory-summary";
import { TrajectoryActivity } from "@/components/trajectory-activity";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { fetcher } from "@/lib/api";
import type {
  Trajectory,
  TrajectoryStep,
  FinalMetrics,
  MessageContent,
  ObservationContent,
} from "@/lib/types";
import {
  contentText,
  isEmptyStep,
  phaseColorVars,
  stepDurationsMs,
  stepPreview,
} from "@/lib/trajectory-metrics";
import {
  groupStatsLabel,
  groupStepsBySegment,
  renderableStepIds,
  toSegments,
  withOtherSegment,
} from "@/lib/trajectory-segments";
import { useTrajectorySummary } from "@/lib/use-trajectory-summary";

import { formatMs } from "@/lib/utils";

function formatStepDuration(
  prevTimestamp: string | null,
  currentTimestamp: string | null
): string | null {
  if (!prevTimestamp || !currentTimestamp) return null;
  const prev = new Date(prevTimestamp).getTime();
  const current = new Date(currentTimestamp).getTime();
  const diff = current - prev;
  if (diff < 0 || Number.isNaN(diff)) return null;
  return formatMs(diff);
}

function getOscillatingColor(index: number): string {
  // Pattern: 1-2-3-4-3-2-1-2-3-4... for visual variety
  const colors = [
    "hsl(var(--muted))",
    "hsl(var(--muted-foreground) / 0.3)",
    "hsl(var(--muted-foreground) / 0.4)",
    "hsl(var(--muted-foreground) / 0.5)",
  ];
  const position = index % 6;
  const colorIndex = position <= 3 ? position : 6 - position;
  return colors[colorIndex];
}

interface ImageError {
  status: number;
  message: string;
}

/**
 * Collect all human-readable text from a step (message, reasoning, tool call
 * names + arguments, and observations) into a single lower-cased string for
 * keyword matching.
 */
function getStepSearchText(step: TrajectoryStep): string {
  const parts: string[] = [contentText(step.message)];

  if (step.reasoning_content) {
    parts.push(step.reasoning_content);
  }

  if (step.tool_calls) {
    for (const tc of step.tool_calls) {
      parts.push(tc.function_name);
      try {
        parts.push(JSON.stringify(tc.arguments));
      } catch {
        // Ignore arguments that can't be stringified (e.g. circular refs).
      }
    }
  }

  if (step.observation) {
    for (const result of step.observation.results) {
      parts.push(contentText(result.content));
    }
  }

  if (step.model_name) {
    parts.push(step.model_name);
  }

  return parts.join("\n").toLowerCase();
}

function stepMatchesQuery(step: TrajectoryStep, lowerQuery: string): boolean {
  if (!lowerQuery) return true;
  return getStepSearchText(step).includes(lowerQuery);
}

function downloadTrajectoryJson(trajectory: Trajectory, trialId: string) {
  const blob = new Blob([JSON.stringify(trajectory, null, 2)], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");

  link.href = url;
  link.download = `trajectory-${trialId}.json`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

function ImageWithFallback({ src, path }: { src: string; path: string }) {
  const [error, setError] = useState<ImageError | null>(null);

  const handleError = async () => {
    try {
      const response = await fetch(src);
      let message = response.statusText || "Failed to load image";
      if (!response.ok) {
        try {
          const json = await response.json();
          message = json.detail || json.error || message;
        } catch {
          // Ignore malformed JSON error payloads.
        }
      }
      setError({ status: response.status, message });
    } catch {
      setError({ status: 0, message: "Network error" });
    }
  };

  if (error) {
    return (
      <div className="my-2">
        <div className="border-muted-foreground/50 bg-muted/50 rounded border border-dashed p-4 text-sm">
          <div className="text-muted-foreground mb-2 flex items-center gap-2">
            <ImageOff className="h-4 w-4" />
            <span className="font-medium">Image unavailable</span>
            {error.status > 0 && (
              <span className="bg-muted rounded px-1.5 py-0.5 text-xs">
                {error.status}
              </span>
            )}
          </div>
          <div className="text-muted-foreground/80 font-mono text-xs break-all">
            {path}
          </div>
          <div className="text-muted-foreground/60 mt-2 text-xs">
            {error.message}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="my-2">
      <img
        src={src}
        alt={`Image: ${path}`}
        className="border-border h-auto max-w-full rounded border"
        style={{ maxHeight: "400px" }}
        loading="lazy"
        onError={handleError}
      />
      <div className="text-muted-foreground mt-1 text-xs">{path}</div>
    </div>
  );
}

function ContentRenderer({
  content,
  trialId,
  apiBaseUrl,
}: {
  content: MessageContent | ObservationContent;
  trialId: string;
  apiBaseUrl: string;
}) {
  if (content === null || content === undefined) {
    return <span className="text-muted-foreground italic">(empty)</span>;
  }

  if (typeof content === "string") {
    return (
      <div className="text-sm wrap-break-word whitespace-pre-wrap">
        {content || (
          <span className="text-muted-foreground italic">(empty)</span>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {content.map((part, idx) => {
        if (part.type === "text") {
          return (
            <div
              key={idx}
              className="text-sm wrap-break-word whitespace-pre-wrap"
            >
              {part.text}
            </div>
          );
        }

        if (part.type === "image" && part.source?.path) {
          const encodedPath = part.source.path
            .split("/")
            .map((segment) => encodeURIComponent(segment))
            .join("/");
          const imageUrl = `${apiBaseUrl}/trials/${encodeURIComponent(trialId)}/files/agent/${encodedPath}`;
          return (
            <ImageWithFallback
              key={idx}
              src={imageUrl}
              path={part.source.path}
            />
          );
        }

        return null;
      })}
    </div>
  );
}

// =============================================================================
// StepDurationBar Component
// =============================================================================

interface StepDurationInfo {
  stepId: number;
  /** Index into the full step list, which is what onStepClick expects. */
  index: number;
  durationMs: number;
  elapsedMs: number;
}

function StepDurationBar({
  steps,
  onStepClick,
}: {
  steps: TrajectoryStep[];
  onStepClick: (index: number) => void;
}) {
  const [hoveredIndex, setHoveredIndex] = useState<number | null>(null);

  // Empty padding steps hold no time and no work; a run of them would otherwise
  // render as hundreds of min-width slices that mean nothing. Time comes from
  // the shared measure over the *full* list, so a slice, the group header above
  // it and the Activity card all charge a padding run to the same step.
  const durations = stepDurationsMs(steps);
  const stepDurations: StepDurationInfo[] = [];
  let elapsedMs = 0;
  steps.forEach((step, index) => {
    elapsedMs += durations[index];
    if (isEmptyStep(step)) return;
    stepDurations.push({
      stepId: step.step_id,
      index,
      durationMs: durations[index],
      elapsedMs,
    });
  });
  if (stepDurations.length === 0) return null;

  const totalMs = stepDurations.reduce((sum, s) => sum + s.durationMs, 0);

  if (totalMs === 0) {
    return (
      <div className="mb-4">
        <div className="bg-muted h-6 rounded" />
      </div>
    );
  }

  // Calculate widths with minimum width for visibility
  const minWidthPercent = 2;
  const rawWidths = stepDurations.map((s) => (s.durationMs / totalMs) * 100);
  const widths = rawWidths.map((w) => Math.max(w, minWidthPercent));

  // Calculate cumulative widths for tooltip positioning
  const cumulativeWidths: number[] = [];
  let cumulative = 0;
  for (const w of widths) {
    cumulativeWidths.push(cumulative);
    cumulative += w;
  }

  return (
    <TooltipProvider>
      <div className="mb-4">
        <div className="relative">
          <div className="flex h-6 overflow-hidden rounded">
            {stepDurations.map((step, idx) => {
              const widthPercent = widths[idx];
              const isHovered = hoveredIndex === idx;
              const isOtherHovered =
                hoveredIndex !== null && hoveredIndex !== idx;

              return (
                <Tooltip key={step.stepId} open={isHovered}>
                  <TooltipTrigger asChild>
                    <div
                      className="cursor-pointer transition-all duration-150 hover:brightness-110"
                      style={{
                        width: `${widthPercent}%`,
                        backgroundColor: getOscillatingColor(idx),
                        opacity: isOtherHovered ? 0.3 : 1,
                        transform: isHovered ? "scaleY(1.1)" : "scaleY(1)",
                      }}
                      onMouseEnter={() => setHoveredIndex(idx)}
                      onMouseLeave={() => setHoveredIndex(null)}
                      onClick={() => onStepClick(step.index)}
                    />
                  </TooltipTrigger>
                  <TooltipContent side="top">
                    <div className="flex flex-col gap-1 text-xs">
                      <div className="font-medium">Step #{step.stepId}</div>
                      <div className="text-muted-foreground">
                        Duration: {formatMs(step.durationMs)}
                      </div>
                      <div className="text-muted-foreground">
                        At: {formatMs(step.elapsedMs)}
                      </div>
                    </div>
                  </TooltipContent>
                </Tooltip>
              );
            })}
          </div>
        </div>
      </div>
    </TooltipProvider>
  );
}

// =============================================================================
// Token Usage Bar Component
// =============================================================================

function TokenUsageBar({ metrics }: { metrics: FinalMetrics | null }) {
  if (!metrics) return null;

  const cached = metrics.total_cached_tokens ?? 0;
  const prompt = metrics.total_prompt_tokens ?? 0;
  const completion = metrics.total_completion_tokens ?? 0;

  // Prompt tokens include cached, so non-cached prompt = prompt - cached
  const nonCachedPrompt = Math.max(0, prompt - cached);
  const total = nonCachedPrompt + cached + completion;

  if (total === 0) return null;

  const segments = [
    { key: "cached", value: cached, color: "bg-emerald-500", label: "Cached" },
    {
      key: "prompt",
      value: nonCachedPrompt,
      color: "bg-blue-500",
      label: "Prompt",
    },
    {
      key: "completion",
      value: completion,
      color: "bg-purple-500",
      label: "Output",
    },
  ].filter((s) => s.value > 0);

  // Calculate widths with minimum for visibility
  const minWidthPercent = 8;
  const widths = segments.map((s) => {
    const raw = (s.value / total) * 100;
    return Math.max(raw, minWidthPercent);
  });

  return (
    <TooltipProvider>
      <div className="mb-4">
        <div className="mb-1.5 flex items-center gap-2">
          <span className="text-muted-foreground text-[10px] tracking-wider uppercase">
            Tokens
          </span>
          <span className="text-muted-foreground text-xs">
            {total.toLocaleString()} total
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
                  {segment.label}: {segment.value.toLocaleString()}
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
                {segment.label}: {segment.value.toLocaleString()}
              </span>
            </div>
          ))}
        </div>
      </div>
    </TooltipProvider>
  );
}

// =============================================================================
// Step Metrics Bar Component (compact version for individual steps)
// =============================================================================

function StepMetricsBar({ metrics }: { metrics: TrajectoryStep["metrics"] }) {
  if (!metrics) return null;

  const cached = metrics.cached_tokens ?? 0;
  const prompt = metrics.prompt_tokens ?? 0;
  const completion = metrics.completion_tokens ?? 0;

  const nonCachedPrompt = Math.max(0, prompt - cached);
  const total = nonCachedPrompt + cached + completion;

  const segments = [
    { key: "cached", value: cached, color: "bg-emerald-500", label: "Cached" },
    {
      key: "prompt",
      value: nonCachedPrompt,
      color: "bg-blue-500",
      label: "Prompt",
    },
    {
      key: "completion",
      value: completion,
      color: "bg-purple-500",
      label: "Output",
    },
  ].filter((s) => s.value > 0);

  return (
    <div className="text-muted-foreground flex items-center gap-3 text-xs">
      {/* Mini token bar */}
      {total > 0 && (
        <div className="flex items-center gap-1.5">
          <div className="flex h-1.5 w-16 gap-px overflow-hidden rounded-full">
            {segments.map((segment) => (
              <div
                key={segment.key}
                className={segment.color}
                style={{ width: `${(segment.value / total) * 100}%` }}
              />
            ))}
          </div>
          <span>{total.toLocaleString()}</span>
        </div>
      )}
      {/* Token breakdown */}
      {segments.map((segment) => (
        <span key={segment.key} className="flex items-center gap-1">
          <span className={`h-1.5 w-1.5 rounded-full ${segment.color}`} />
          {segment.value.toLocaleString()}
        </span>
      ))}
      {/* Cost */}
      {metrics.cost_usd && metrics.cost_usd > 0 && (
        <span className="font-medium text-green-500">
          ${metrics.cost_usd.toFixed(4)}
        </span>
      )}
    </div>
  );
}

// =============================================================================
// StepTrigger Component
// =============================================================================

function StepTrigger({
  step,
  durationMs,
  startTimestamp,
}: {
  step: TrajectoryStep;
  /** From the shared measure, not a delta against the row above: padding is
   *  charged to the next step that did work, so a neighbour delta here would
   *  contradict the group header. Null when there is nothing to charge. */
  durationMs: number | null;
  startTimestamp: string | null;
}) {
  const stepDuration =
    durationMs != null && step.timestamp ? formatMs(durationMs) : null;
  const sinceStart = formatStepDuration(startTimestamp, step.timestamp);
  const firstLine = stepPreview(step)?.slice(0, 60) || null;

  return (
    <StepHeader
      index={step.step_id}
      source={step.source}
      model={step.model_name}
      preview={firstLine}
      badges={
        stepDuration || sinceStart ? (
          <>
            {stepDuration && (
              <Badge
                variant="secondary"
                className="px-1.5 py-0 text-[10px] font-normal"
              >
                +{stepDuration}
              </Badge>
            )}
            {sinceStart && (
              <Badge
                variant="outline"
                className="px-1.5 py-0 text-[10px] font-normal"
              >
                @{sinceStart}
              </Badge>
            )}
          </>
        ) : undefined
      }
    />
  );
}

// =============================================================================
// StepContent Component
// =============================================================================

function StepContent({
  step,
  trialId,
  apiBaseUrl,
}: {
  step: TrajectoryStep;
  trialId: string;
  apiBaseUrl: string;
}) {
  return (
    <div className="space-y-3 text-sm">
      {/* Message */}
      {step.message && (
        <ContentRenderer
          content={step.message}
          trialId={trialId}
          apiBaseUrl={apiBaseUrl}
        />
      )}

      {/* Reasoning */}
      {step.reasoning_content && (
        <div>
          <h5 className="text-muted-foreground mb-1 text-xs font-medium">
            Reasoning
          </h5>
          <div className="rounded border border-blue-500/20 bg-blue-500/10 p-2 text-xs whitespace-pre-wrap">
            {step.reasoning_content}
          </div>
        </div>
      )}

      {/* Tool Calls */}
      {step.tool_calls && step.tool_calls.length > 0 && (
        <div>
          <h5 className="text-muted-foreground mb-1 text-xs font-medium">
            Tool Calls
          </h5>
          <div className="space-y-2">
            {step.tool_calls.map((tc) => (
              <ToolCallBlock
                key={tc.tool_call_id}
                name={tc.function_name}
                args={JSON.stringify(tc.arguments, null, 2)}
              />
            ))}
          </div>
        </div>
      )}

      {/* Observations */}
      {step.observation && step.observation.results.length > 0 && (
        <div>
          <h5 className="text-muted-foreground mb-1 text-xs font-medium">
            Observations
          </h5>
          <div className="space-y-2">
            {step.observation.results.map((result, idx) => {
              const text = contentText(result.content);
              const hasMultimodalContent =
                !!result.content &&
                typeof result.content !== "string" &&
                result.content.some((part) => part.type === "image");

              if (!hasMultimodalContent) {
                return (
                  <ObservationBlock key={idx} content={text || "(empty)"} />
                );
              }

              return (
                <div
                  key={idx}
                  className="border-border/60 bg-muted/20 rounded border p-2"
                >
                  <ContentRenderer
                    content={result.content}
                    trialId={trialId}
                    apiBaseUrl={apiBaseUrl}
                  />
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Metrics */}
      {step.metrics && (
        <div className="border-border/50 border-t pt-2">
          <StepMetricsBar metrics={step.metrics} />
        </div>
      )}
    </div>
  );
}

// =============================================================================
// Main TrajectoryViewer Component
// =============================================================================

interface TrajectoryViewerProps {
  trialId: string;
  /**
   * Whether the backend recorded an ATIF trajectory for this trial
   * (mirrors ``TrialResponse.has_trajectory``).  When ``false`` we skip
   * the fetch entirely — the endpoint would just return ``null`` after
   * a multi-second S3 probe, and some trials (older rows with a stale
   * ``harbor_result_path`` pointing at the decommissioned Modal volume)
   * additionally surface a spurious 403 on the local-fallback branch.
   * ``undefined`` preserves legacy behaviour (always fetch) for
   * consumers that haven't been updated.
   */
  hasTrajectory?: boolean;
  apiBaseUrl?: string;
}

export function TrajectoryViewer({
  trialId,
  hasTrajectory,
  apiBaseUrl = "/api",
}: TrajectoryViewerProps) {
  const shouldFetch = hasTrajectory !== false;
  const {
    data: trajectory,
    isLoading,
    error,
  } = useSWR<Trajectory | null>(
    shouldFetch ? `${apiBaseUrl}/trials/${trialId}/trajectory` : null,
    fetcher,
    {
      revalidateOnFocus: false,
    }
  );

  const [expandedSteps, setExpandedSteps] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [deepLinkError, setDeepLinkError] = useState<string | null>(null);
  const stepRefs = useRef<(HTMLDivElement | null)[]>([]);
  const stepReset = useRef<string | null>(null);
  // The step a #step-<id> address asked for, held here once the address has
  // been relieved of it (see the capture effect below), TAGGED WITH THE TRIAL
  // it arrived for. The tag is the correctness property, not the reset below:
  // clearing on a trial switch is a state update, so the honour effect still
  // runs this flush with the old step, and re-runs because `trajectory` just
  // changed -- handing the step being left to the trial arriving. With the
  // tag, honouring is conditional on the trial matching, so no ordering
  // between the two effects can apply a step to the wrong run.
  const [pendingStep, setPendingStep] = useState<{
    trial: string;
    step: number;
  } | null>(null);
  // Read by the capture effect, which mounts once and would otherwise close
  // over the mount-time trial for every later hashchange.
  const trialIdRef = useRef(trialId);
  trialIdRef.current = trialId;

  // Reset expanded steps and search when switching to a different trial
  useEffect(() => {
    if (trialId !== stepReset.current) {
      const switched = stepReset.current !== null;
      stepReset.current = trialId;
      setExpandedSteps([]);
      setQuery("");
      // A deep link belongs to the trial it arrived with. Trials are switched
      // in place from the drawer's own list, which never carries a fragment,
      // so a step still pending on a switch was addressed to the trial being
      // left. Guarded on `switched` so this cannot wipe the step captured on
      // the first mount, whatever order the two effects run in.
      //
      // The failure message goes with it. Nothing re-runs the link against the
      // new trial — the fragment was spent when it was captured — so a message
      // left standing would describe a run the reader is no longer looking at.
      if (switched) {
        setPendingStep(null);
        setDeepLinkError(null);
      }
    }
  }, [trialId]);

  // Take #step-<step_id> out of the address as soon as this viewer mounts, and
  // hold the step until the trajectory can honour it.
  //
  // The fragment addresses one step of one run, and nothing else reads it, so
  // its life ends here. It cannot be left parked until the trajectory
  // resolves: urlWithSearch carries the fragment through every panel
  // replaceState, so a run that never yields steps — no trajectory, a failed
  // fetch, a drawer closed mid-flight — would hand its anchor to whichever
  // trial is opened next. Spending it on sight separates "the link has been
  // used" from "the link could be honoured".
  //
  // Reading it at mount is unambiguous because citations are plain anchors:
  // they arrive as a document navigation with the fragment and ?trial= already
  // agreeing. No in-app navigation changes both, so the listener below only
  // ever sees the fragment move within one trial.
  useEffect(() => {
    const take = () => {
      const m = /^#step-(\d+)$/.exec(window.location.hash);
      if (!m) return;
      setPendingStep({ trial: trialIdRef.current, step: Number(m[1]) });
      const spent = `${window.location.pathname}${window.location.search}`;
      window.history.replaceState(window.history.state, "", spent);
    };
    take();
    window.addEventListener("hashchange", take);
    return () => window.removeEventListener("hashchange", take);
  }, []);

  // Steps keep their original index so timing, refs, and duration-bar clicks
  // stay consistent with the full trajectory.
  const lowerQuery = query.trim().toLowerCase();
  // Padding steps hold nothing to read — gemini-cli exports are mostly these —
  // so the list drops them the way the duration bar, segments and Activity card
  // already do.
  const renderedSteps = useMemo(
    () =>
      (trajectory?.steps ?? [])
        .map((step, idx) => ({ step, idx }))
        .filter(({ step }) => !isEmptyStep(step)),
    [trajectory]
  );
  const visibleSteps = useMemo(() => {
    if (!lowerQuery) return renderedSteps;
    return renderedSteps.filter(({ step }) =>
      stepMatchesQuery(step, lowerQuery)
    );
  }, [renderedSteps, lowerQuery]);

  // A summary request can trigger paid on-demand generation server-side, so it
  // must not fire for a trial we already know (via shouldFetch) has no trajectory.
  const { data: summary } = useTrajectorySummary(
    trialId,
    apiBaseUrl,
    shouldFetch
  );
  // Derived from the whole trajectory, so attribution stays put while the user
  // searches, and shared with the Activity card so both agree on every owner.
  const renderableIds = useMemo(
    () => renderableStepIds(trajectory?.steps ?? []),
    [trajectory]
  );
  // Coverage runs over what the list actually draws. Passing the full
  // trajectory here would hand every padding step to Other -- the summariser
  // stopped claiming them in #1155, so unclaimed is exactly the padding.
  const segments = useMemo(
    () =>
      withOtherSegment(
        toSegments(summary),
        renderedSteps.map(({ step }) => step),
        renderableIds
      ),
    [summary, renderedSteps, renderableIds]
  );
  const colorFor = useMemo(
    () => phaseColorVars(segments.map((s) => s.key)),
    [segments]
  );
  // Grouping runs over the *filtered* list, so a group whose steps all filtered
  // out is simply never emitted.
  const groups = useMemo(
    () => groupStepsBySegment(visibleSteps, segments, renderableIds),
    [visibleSteps, segments, renderableIds]
  );
  // Full-trajectory durations: group steps carry indexes into the full list.
  const stepDurations = useMemo(
    () => stepDurationsMs(trajectory?.steps ?? []),
    [trajectory]
  );

  // Every jump target resolves through here, so a step the list does not draw
  // reports -1 and the caller shows it as unavailable rather than scrolling to
  // nothing. Summaries stored before #1155 can still cite padding.
  const stepIdToIndex = useCallback(
    (stepId: number) =>
      // step_id is typed number but arrives as a string from some producers;
      // strict === would return -1 and the jump would silently no-op.
      renderedSteps.find(({ step }) => Number(step.step_id) === Number(stepId))
        ?.idx ?? -1,
    [renderedSteps]
  );

  const handleStepClick = useCallback(
    (index: number) => {
      // Callers resolve targets through stepIdToIndex, so this only catches a
      // caller that skipped it: expanding a step the list never draws would
      // clear the search filter and then scroll nowhere.
      if (!renderedSteps.some(({ idx }) => idx === index)) return;
      const stepKey = `step-${index}`;
      // The duration bar spans every step, so a click may target a step the
      // active filter is hiding — clear the filter so it can be shown.
      if (lowerQuery && !visibleSteps.some(({ idx }) => idx === index)) {
        setQuery("");
      }
      setExpandedSteps((prev) =>
        prev.includes(stepKey) ? prev : [...prev, stepKey]
      );
      // Scroll to step after a brief delay for accordion animation
      setTimeout(() => {
        stepRefs.current[index]?.scrollIntoView({
          behavior: "smooth",
          block: "start",
        });
      }, 50);
    },
    [lowerQuery, visibleSteps, renderedSteps]
  );

  // Honour a captured deep link once the trajectory has loaded. It carries a
  // step_id, not an array index; handleStepClick takes an index. Clearing it
  // first makes it single-use, so a later pass — this effect re-runs whenever
  // the search box changes handleStepClick — cannot scroll the reader back.
  useEffect(() => {
    const steps = trajectory?.steps;
    if (pendingStep === null || !steps?.length) return;
    // Not this trial's step: leave it alone. It belongs to the run the reader
    // left, and the reset effect clears it on the next render.
    if (pendingStep.trial !== trialId) return;
    const step = pendingStep.step;
    setPendingStep(null);
    const idx = stepIdToIndex(step);
    if (idx >= 0) {
      setDeepLinkError(null);
      handleStepClick(idx);
    } else if (steps.some((s) => Number(s.step_id) === step)) {
      // Present but empty: the list never draws it, so expanding the item
      // would scroll to nothing.
      setDeepLinkError(`Step ${step} is empty and is not shown.`);
    } else {
      setDeepLinkError(`Step ${step} is not in this trajectory.`);
    }
  }, [pendingStep, trialId, trajectory, handleStepClick, stepIdToIndex]);

  if (isLoading) {
    return (
      <div className="space-y-3 p-4">
        <Skeleton className="h-6 w-full" />
        <Skeleton className="h-16 w-full" />
        <Skeleton className="h-16 w-full" />
        <Skeleton className="h-16 w-full" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-4 text-center">
        <Route className="mx-auto mb-2 h-8 w-8 text-red-500" />
        <p className="text-muted-foreground text-sm">
          Failed to load trajectory
        </p>
        <p className="mt-1 text-xs text-red-500">{error.message}</p>
      </div>
    );
  }

  if (!trajectory) {
    return (
      <div className="p-6 text-center">
        <Route className="text-muted-foreground/50 mx-auto mb-3 h-10 w-10" />
        <p className="text-muted-foreground text-sm font-medium">
          No trajectory available
        </p>
        <p className="text-muted-foreground/70 mx-auto mt-1 max-w-xs text-xs">
          This trial doesn't have ATIF trajectory data. Trajectories are
          recorded for agents that support the ATIF format.
        </p>
      </div>
    );
  }

  return (
    <div className="p-4">
      <TrajectorySummary
        trialId={trialId}
        apiBaseUrl={apiBaseUrl}
        renderableIds={renderableIds}
        stepIdToIndex={stepIdToIndex}
        onStepSelect={handleStepClick}
      />
      <TrajectoryActivity
        trialId={trialId}
        steps={trajectory.steps}
        apiBaseUrl={apiBaseUrl}
        stepIdToIndex={stepIdToIndex}
        onStepSelect={handleStepClick}
      />
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center justify-between text-sm font-medium">
            <span className="flex items-center gap-2">
              <Route className="h-4 w-4" />
              Trajectory
            </span>
            <span className="flex items-center gap-2">
              <span className="text-muted-foreground text-xs font-normal">
                {lowerQuery
                  ? `${visibleSteps.length} of ${renderedSteps.length} steps`
                  : `${renderedSteps.length} steps`}
                {trajectory.final_metrics?.total_cost_usd && (
                  <> · ${trajectory.final_metrics.total_cost_usd.toFixed(4)}</>
                )}
              </span>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-7 px-2 text-xs"
                onClick={() => downloadTrajectoryJson(trajectory, trialId)}
              >
                <Download className="h-3.5 w-3.5" />
                Export JSON
              </Button>
            </span>
          </CardTitle>
        </CardHeader>
        <CardContent className="overflow-x-auto pt-1">
          {deepLinkError && (
            <div className="text-muted-foreground mb-2 text-sm">
              {deepLinkError}
            </div>
          )}
          {/* Step search */}
          <div className="relative mb-4">
            <Search className="text-muted-foreground pointer-events-none absolute top-1/2 left-2.5 h-3.5 w-3.5 -translate-y-1/2" />
            <Input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Filter steps by keyword…"
              className="h-9 pr-8 pl-8 text-sm"
            />
            {query && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => setQuery("")}
                aria-label="Clear search"
                className="text-muted-foreground hover:text-foreground absolute top-1/2 right-1 h-7 w-7 -translate-y-1/2 p-0"
              >
                <X className="h-3.5 w-3.5" />
              </Button>
            )}
          </div>

          {/* Token Usage Bar */}
          <TokenUsageBar metrics={trajectory.final_metrics} />

          {/* Duration Bar */}
          <StepDurationBar
            steps={trajectory.steps}
            onStepClick={handleStepClick}
          />

          {/* Steps Accordion */}
          {visibleSteps.length === 0 ? (
            <div className="text-muted-foreground py-8 text-center text-sm">
              {lowerQuery ? (
                <>
                  No steps match{" "}
                  <span className="text-foreground font-medium">
                    &ldquo;{query.trim()}&rdquo;
                  </span>
                </>
              ) : (
                "This trajectory has no steps with content."
              )}
            </div>
          ) : (
            <Accordion
              type="multiple"
              value={expandedSteps}
              onValueChange={setExpandedSteps}
            >
              {groups.map((group) => (
                // Keyed on the group's first step index, never positionally: the
                // summary resolves after the trajectory, so the group count — and
                // every index-derived key — changes when it arrives. A positional
                // key would remount every AccordionItem/StepContent below it.
                <div
                  key={`${group.key ?? "unclaimed"}-${group.steps[0].idx}`}
                  className="mt-5 first:mt-0"
                >
                  {group.label && (
                    <div className="flex items-center gap-2 border-b pb-1.5">
                      <span
                        className="h-4 w-1 rounded-sm"
                        aria-hidden="true"
                        style={{
                          background:
                            (group.key && colorFor.get(group.key)) ??
                            "var(--phase-other)",
                        }}
                      />
                      <span
                        role="heading"
                        aria-level={4}
                        className="text-sm font-semibold"
                      >
                        {group.label}
                      </span>
                      <span className="text-muted-foreground ml-auto font-mono text-xs">
                        {groupStatsLabel(group, stepDurations)}
                      </span>
                    </div>
                  )}
                  {group.gist && (
                    <p className="text-muted-foreground pt-1.5 pb-1 text-xs">
                      {group.gist}
                    </p>
                  )}
                  {group.steps.map(({ step, idx }) => (
                    <AccordionItem
                      key={step.step_id}
                      value={`step-${idx}`}
                      ref={(el: HTMLDivElement | null) => {
                        stepRefs.current[idx] = el;
                      }}
                    >
                      <AccordionTrigger className="py-3 hover:no-underline">
                        <StepTrigger
                          step={step}
                          durationMs={
                            idx > 0 ? (stepDurations[idx] ?? null) : null
                          }
                          startTimestamp={
                            trajectory.steps[0]?.timestamp ?? null
                          }
                        />
                      </AccordionTrigger>
                      <AccordionContent>
                        <StepContent
                          step={step}
                          trialId={trialId}
                          apiBaseUrl={apiBaseUrl}
                        />
                      </AccordionContent>
                    </AccordionItem>
                  ))}
                </div>
              ))}
            </Accordion>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

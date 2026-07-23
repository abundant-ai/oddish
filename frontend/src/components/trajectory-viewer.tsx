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
import {
  Route,
  Download,
  ImageOff,
  Search,
  X,
  Star,
  Undo2,
  Link2,
  Check,
} from "lucide-react";
import {
  StepHeader,
  ToolCallBlock,
  ObservationBlock,
} from "@/components/trajectory-blocks";
import { TrajectorySummary } from "@/components/trajectory-summary";
import { TrajectoryScrubber } from "@/components/trajectory-scrubber";
import { fetcher } from "@/lib/api";
import type {
  Trajectory,
  TrajectoryStep,
  MessageContent,
  ObservationContent,
  ContentPart,
} from "@/lib/types";
import {
  phaseColorVars,
  stepDurationsMs,
  stepErrorFlags,
} from "@/lib/trajectory-metrics";
import {
  groupStatsLabel,
  groupStepsBySegment,
  segmentOwners,
  toSegments,
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

/** Nearest ancestor that actually scrolls (the drawer's tab body). */
function getScrollParent(el: HTMLElement | null): HTMLElement | null {
  let node = el?.parentElement ?? null;
  while (node) {
    const { overflowY } = window.getComputedStyle(node);
    if (overflowY === "auto" || overflowY === "scroll") return node;
    node = node.parentElement;
  }
  return null;
}

interface ImageError {
  status: number;
  message: string;
}

function getTextFromContent(
  content: MessageContent | ObservationContent
): string {
  if (content === null || content === undefined) {
    return "";
  }
  if (typeof content === "string") {
    return content;
  }

  return content
    .filter(
      (part): part is ContentPart & { type: "text" } => part.type === "text"
    )
    .map((part) => part.text || "")
    .join("\n");
}

function getFirstLine(
  content: MessageContent | ObservationContent
): string | null {
  const text = getTextFromContent(content);
  return text?.split("\n")[0] || null;
}

/**
 * Collect all human-readable text from a step (message, reasoning, tool call
 * names + arguments, and observations) into a single lower-cased string for
 * keyword matching.
 */
function getStepSearchText(step: TrajectoryStep): string {
  const parts: string[] = [getTextFromContent(step.message)];

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
      parts.push(getTextFromContent(result.content));
    }
  }

  if (step.model_name) {
    parts.push(step.model_name);
  }

  return parts.join("\n").toLowerCase();
}

function stepMatchesQuery(
  step: TrajectoryStep,
  lowerQuery: string,
  extraText?: string
): boolean {
  if (!lowerQuery) return true;
  if (extraText && extraText.includes(lowerQuery)) return true;
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
  prevTimestamp,
  startTimestamp,
  isHighlight,
}: {
  step: TrajectoryStep;
  prevTimestamp: string | null;
  startTimestamp: string | null;
  isHighlight?: boolean;
}) {
  const stepDuration = formatStepDuration(prevTimestamp, step.timestamp);
  const sinceStart = formatStepDuration(startTimestamp, step.timestamp);
  const firstLine = getFirstLine(step.message)?.slice(0, 60) || null;

  return (
    <StepHeader
      index={step.step_id}
      source={step.source}
      model={step.model_name}
      preview={firstLine}
      badges={
        isHighlight || stepDuration || sinceStart ? (
          <>
            {isHighlight && (
              <Star
                aria-label="Key step"
                className="h-3 w-3 fill-amber-400 text-amber-500"
              />
            )}
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
              const text = getTextFromContent(result.content);
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
  const [activeIndex, setActiveIndex] = useState<number | null>(null);
  const [flashIndex, setFlashIndex] = useState<number | null>(null);
  // Scroll offset to restore via the "Back" pill after a jump (back-linking).
  const [returnScroll, setReturnScroll] = useState<number | null>(null);
  const [copiedStepId, setCopiedStepId] = useState<number | null>(null);
  const stepRefs = useRef<(HTMLDivElement | null)[]>([]);
  const overviewRef = useRef<HTMLDivElement | null>(null);
  const scrubberRef = useRef<HTMLDivElement | null>(null);
  const stepReset = useRef<string | null>(null);
  const appliedHash = useRef<string | null>(null);
  const flashTimer = useRef<number | null>(null);
  const copyTimer = useRef<number | null>(null);

  // Reset per-trial UI state when switching to a different trial
  useEffect(() => {
    if (trialId !== stepReset.current) {
      const previousTrial = stepReset.current;
      stepReset.current = trialId;
      setExpandedSteps([]);
      setQuery("");
      setActiveIndex(null);
      setFlashIndex(null);
      setReturnScroll(null);
      setCopiedStepId(null);
      // Otherwise an unchanged hash across trials (e.g. both #step-1) would
      // look "already applied" and the deep link would silently no-op.
      appliedHash.current = null;
      // A #step-N hash belongs to the trial it was opened for. Drop it when
      // NAVIGATING to a different trial (not on first mount, where it's the
      // deep link we're about to apply) so it doesn't re-fire on every trial
      // the user arrows through.
      if (previousTrial !== null && /^#step-\d+$/.test(window.location.hash)) {
        window.history.replaceState(
          window.history.state,
          "",
          `${window.location.pathname}${window.location.search}`
        );
      }
    }
  }, [trialId]);

  // A summary request can trigger paid on-demand generation server-side, so it
  // must not fire for a trial we already know (via shouldFetch) has no trajectory.
  const { data: summary } = useTrajectorySummary(
    trialId,
    apiBaseUrl,
    shouldFetch
  );
  const segments = useMemo(() => toSegments(summary), [summary]);
  const colorFor = useMemo(
    () => phaseColorVars(segments.map((s) => s.key)),
    [segments]
  );
  // step_id → owning segment; drives the scrubber track, the per-step
  // colored edge, and the segment back-link chip.
  const ownerByStep = useMemo(() => segmentOwners(segments), [segments]);
  const highlightById = useMemo(() => {
    const map = new Map<number, { title: string; why: string }>();
    for (const h of summary?.highlights ?? [])
      map.set(Number(h.step_id), { title: h.title, why: h.why });
    return map;
  }, [summary]);
  const errorFlags = useMemo(
    () => stepErrorFlags(trajectory?.steps ?? []),
    [trajectory]
  );

  // Summary-derived text per step (component label + gist, highlight title +
  // why), folded into the search corpus so e.g. typing "debugging" pulls up
  // that component's steps even when the raw step text never uses the word.
  const searchExtraByStep = useMemo(() => {
    const map = new Map<number, string>();
    for (const step of trajectory?.steps ?? []) {
      const id = Number(step.step_id);
      const seg = ownerByStep.get(id);
      const h = highlightById.get(id);
      const extra = [seg?.label, seg?.gist, h?.title, h?.why]
        .filter(Boolean)
        .join("\n");
      if (extra) map.set(id, extra.toLowerCase());
    }
    return map;
  }, [trajectory, ownerByStep, highlightById]);

  // Filter steps by keyword, keeping each step's original index so timing,
  // refs, and scrubber clicks stay consistent with the full trajectory.
  const lowerQuery = query.trim().toLowerCase();
  const visibleSteps = useMemo(() => {
    const all = (trajectory?.steps ?? []).map((step, idx) => ({ step, idx }));
    if (!lowerQuery) return all;
    return all.filter(({ step }) =>
      stepMatchesQuery(
        step,
        lowerQuery,
        searchExtraByStep.get(Number(step.step_id))
      )
    );
  }, [trajectory, lowerQuery, searchExtraByStep]);

  // While a filter is active the scrubber dims non-matching segments so the
  // track and the filtered list tell the same story. Null = no filter.
  const matchedIndices = useMemo(
    () => (lowerQuery ? new Set(visibleSteps.map(({ idx }) => idx)) : null),
    [lowerQuery, visibleSteps]
  );

  // Grouping runs over the *filtered* list, so a group whose steps all filtered
  // out is simply never emitted.
  const groups = useMemo(
    () => groupStepsBySegment(visibleSteps, segments),
    [visibleSteps, segments]
  );
  // Full-trajectory durations: group steps carry indexes into the full list.
  const stepDurations = useMemo(
    () => stepDurationsMs(trajectory?.steps ?? []),
    [trajectory]
  );

  const flashStep = useCallback((index: number) => {
    setFlashIndex(index);
    if (flashTimer.current) window.clearTimeout(flashTimer.current);
    flashTimer.current = window.setTimeout(() => setFlashIndex(null), 1600);
  }, []);

  const handleStepClick = useCallback(
    (index: number) => {
      const stepKey = `step-${index}`;
      // The scrubber spans every step, so a click may target a step the
      // active filter is hiding — clear the filter so it can be shown.
      if (lowerQuery && !visibleSteps.some(({ idx }) => idx === index)) {
        setQuery("");
      }
      setExpandedSteps((prev) =>
        prev.includes(stepKey) ? prev : [...prev, stepKey]
      );
      // Remember where the reader was so the "Back" pill can return there.
      const scroller = getScrollParent(scrubberRef.current);
      if (scroller) setReturnScroll(scroller.scrollTop);
      flashStep(index);
      // Instant snap once the accordion mounts, plus one re-snap after
      // content-visibility rows above resolve their real heights. The
      // playhead + active-row tint carry exact correspondence from there.
      setTimeout(() => {
        stepRefs.current[index]?.scrollIntoView({ block: "start" });
        window.setTimeout(() => {
          stepRefs.current[index]?.scrollIntoView({ block: "start" });
        }, 250);
      }, 50);
    },
    [lowerQuery, visibleSteps, flashStep]
  );

  const scrollToOverview = useCallback(() => {
    overviewRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, []);

  const handleBack = useCallback(() => {
    if (returnScroll == null) return;
    const scroller = getScrollParent(scrubberRef.current);
    scroller?.scrollTo({ top: returnScroll, behavior: "smooth" });
    setReturnScroll(null);
  }, [returnScroll]);

  const copyStepLink = useCallback((stepId: number) => {
    const url = `${window.location.origin}${window.location.pathname}${window.location.search}#step-${stepId}`;
    void navigator.clipboard?.writeText(url).then(() => {
      setCopiedStepId(stepId);
      if (copyTimer.current) window.clearTimeout(copyTimer.current);
      copyTimer.current = window.setTimeout(() => setCopiedStepId(null), 1600);
    });
  }, []);

  // Scroll spy: track which step sits under the sticky scrubber so its
  // playhead follows the reader (and clicking the track scrubs the list).
  useEffect(() => {
    if (!trajectory?.steps?.length) return;
    const scroller = getScrollParent(scrubberRef.current);
    const target: HTMLElement | Window = scroller ?? window;
    let raf = 0;

    const update = () => {
      raf = 0;
      const barBottom = scrubberRef.current?.getBoundingClientRect().bottom ?? 0;
      let current: number | null = null;
      for (let i = 0; i < stepRefs.current.length; i++) {
        const el = stepRefs.current[i];
        if (!el || !el.isConnected) continue;
        const rect = el.getBoundingClientRect();
        if (rect.top <= barBottom + 16) {
          current = i;
        } else {
          break;
        }
      }
      // At the bottom of the pane the last steps can never reach the top, so
      // the topmost-row rule would leave the playhead stuck short of the end.
      // Once the scroller is fully scrolled, point at the last step.
      if (
        scroller &&
        scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2
      ) {
        for (let i = stepRefs.current.length - 1; i >= 0; i--) {
          if (stepRefs.current[i]?.isConnected) {
            current = i;
            break;
          }
        }
      }
      setActiveIndex(current);
    };

    const onScroll = () => {
      if (!raf) raf = requestAnimationFrame(update);
    };

    update();
    target.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      target.removeEventListener("scroll", onScroll);
      if (raf) cancelAnimationFrame(raf);
    };
  }, [trajectory, visibleSteps]);

  // Resolve a #step-<step_id> hash once the trajectory has loaded. The hash
  // carries a step_id, not an array index; handleStepClick takes an index.
  useEffect(() => {
    const steps = trajectory?.steps;
    if (!steps?.length) return;

    const apply = () => {
      const hash = window.location.hash;
      if (hash === appliedHash.current) return;
      const m = /^#step-(\d+)$/.exec(hash);
      if (!m) return;
      appliedHash.current = hash;
      const idx = steps.findIndex((s) => Number(s.step_id) === Number(m[1]));
      if (idx >= 0) {
        setDeepLinkError(null);
        handleStepClick(idx);
      } else {
        setDeepLinkError(`Step ${m[1]} is not in this trajectory.`);
      }
    };

    apply();
    window.addEventListener("hashchange", apply);
    return () => window.removeEventListener("hashchange", apply);
  }, [trajectory, handleStepClick]);

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

  const totalTokens =
    (trajectory.final_metrics?.total_prompt_tokens ?? 0) +
    (trajectory.final_metrics?.total_completion_tokens ?? 0);

  return (
    <div className="p-4 pt-0">
      {/* Sticky scrubber — always in reach while reading the step list. */}
      <div
        ref={scrubberRef}
        className="bg-background/95 supports-[backdrop-filter]:bg-background/80 sticky top-0 z-10 -mx-4 border-b px-4 pt-3 pb-1 backdrop-blur"
      >
        <TrajectoryScrubber
          steps={trajectory.steps}
          errorFlags={errorFlags}
          ownerByStep={ownerByStep}
          colorFor={colorFor}
          highlights={highlightById}
          activeIndex={activeIndex}
          matchedIndices={matchedIndices}
          onStepSelect={handleStepClick}
        />
        {returnScroll != null && (
          <Button
            type="button"
            variant="secondary"
            size="sm"
            onClick={handleBack}
            className="absolute top-1.5 right-4 z-20 h-6 gap-1 px-2 text-[11px] shadow-sm"
          >
            <Undo2 className="h-3 w-3" />
            Back
          </Button>
        )}
      </div>

      <div ref={overviewRef} className="scroll-mt-16 pt-3">
        <TrajectorySummary
          trialId={trialId}
          apiBaseUrl={apiBaseUrl}
          stepIdToIndex={(stepId) =>
            // step_id is typed number but arrives as a string from some producers;
            // strict === would return -1 and the scroll would silently no-op.
            trajectory.steps.findIndex(
              (s) => Number(s.step_id) === Number(stepId)
            )
          }
          onStepSelect={handleStepClick}
        />
      </div>
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
                  ? `${visibleSteps.length} of ${trajectory.steps.length} steps`
                  : `${trajectory.steps.length} steps`}
                {totalTokens > 0 && <> · {totalTokens.toLocaleString()} tok</>}
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

          {/* Steps Accordion */}
          {visibleSteps.length === 0 ? (
            <div className="text-muted-foreground py-8 text-center text-sm">
              No steps match{" "}
              <span className="text-foreground font-medium">
                &ldquo;{query.trim()}&rdquo;
              </span>
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
                  {group.steps.map(({ step, idx }) => {
                    const segment = ownerByStep.get(Number(step.step_id));
                    const segmentColor = segment
                      ? (colorFor.get(segment.key) ?? "var(--phase-other)")
                      : undefined;
                    const highlight = highlightById.get(Number(step.step_id));
                    return (
                      <AccordionItem
                        key={step.step_id}
                        value={`step-${idx}`}
                        ref={(el: HTMLDivElement | null) => {
                          stepRefs.current[idx] = el;
                        }}
                        style={{
                          borderLeftColor: segmentColor ?? "transparent",
                        }}
                        className={
                          // content-visibility keeps very long trajectories
                          // cheap: offscreen collapsed rows skip layout/paint.
                          "scroll-mt-16 border-l-2 pl-2 transition-colors [content-visibility:auto] [contain-intrinsic-size:auto_48px] " +
                          (flashIndex === idx
                            ? "ring-primary/50 rounded-md ring-2 "
                            : "") +
                          // The step the scrubber's playhead points at — so
                          // the track and the list stay visibly in sync.
                          (activeIndex === idx ? "bg-muted" : "")
                        }
                      >
                        <AccordionTrigger className="py-3 hover:no-underline">
                          <StepTrigger
                            step={step}
                            prevTimestamp={
                              idx > 0
                                ? (trajectory.steps[idx - 1]?.timestamp ?? null)
                                : null
                            }
                            startTimestamp={
                              trajectory.steps[0]?.timestamp ?? null
                            }
                            isHighlight={!!highlight}
                          />
                        </AccordionTrigger>
                        <AccordionContent>
                          {/* Back-links: this step's component (→ overview),
                              the reason the summary flagged it, and a
                              shareable #step-N link. */}
                          <div className="mb-3 flex flex-wrap items-center gap-2 text-xs">
                            {segment && (
                              <button
                                type="button"
                                onClick={scrollToOverview}
                                title="Show in overview"
                                className="border-border hover:bg-muted flex items-center gap-1.5 rounded-full border px-2 py-0.5"
                              >
                                <span
                                  className="h-2 w-2 rounded-sm"
                                  style={{ background: segmentColor }}
                                />
                                {segment.label}
                              </button>
                            )}
                            <button
                              type="button"
                              onClick={() =>
                                copyStepLink(Number(step.step_id))
                              }
                              title="Copy link to this step"
                              className="border-border text-muted-foreground hover:bg-muted hover:text-foreground flex items-center gap-1.5 rounded-full border px-2 py-0.5"
                            >
                              {copiedStepId === Number(step.step_id) ? (
                                <>
                                  <Check className="h-3 w-3 text-emerald-500" />
                                  Copied
                                </>
                              ) : (
                                <>
                                  <Link2 className="h-3 w-3" />
                                  Copy link
                                </>
                              )}
                            </button>
                            {highlight && (
                              <span className="text-muted-foreground flex w-full items-start gap-1.5">
                                <Star className="mt-0.5 h-3 w-3 shrink-0 fill-amber-400 text-amber-500" />
                                {/* Wraps (flex-1 + overflow-wrap), never
                                    truncates — a clipped "why" is unreadable. */}
                                <span className="min-w-0 flex-1 leading-snug whitespace-normal [overflow-wrap:anywhere]">
                                  <span className="text-foreground font-medium">
                                    {highlight.title}
                                  </span>
                                  {highlight.why && <> — {highlight.why}</>}
                                </span>
                              </span>
                            )}
                          </div>
                          <StepContent
                            step={step}
                            trialId={trialId}
                            apiBaseUrl={apiBaseUrl}
                          />
                        </AccordionContent>
                      </AccordionItem>
                    );
                  })}
                </div>
              ))}
            </Accordion>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

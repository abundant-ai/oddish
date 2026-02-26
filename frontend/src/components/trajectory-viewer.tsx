"use client";

import { useState, useRef, useEffect, useMemo, useCallback } from "react";
import useSWR from "swr";
import DOMPurify from "dompurify";
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
import { Copy, Check, Route, ChevronRight, ImageOff } from "lucide-react";
import { fetcher } from "@/lib/api";
import type {
  Trajectory,
  TrajectoryStep,
  FinalMetrics,
  MessageContent,
  ObservationContent,
  ContentPart,
} from "@/lib/types";

// =============================================================================
// Utility Functions
// =============================================================================

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

function formatStepDuration(
  prevTimestamp: string | null,
  currentTimestamp: string | null,
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

function getTextFromContent(
  content: MessageContent | ObservationContent,
): string {
  if (content === null || content === undefined) {
    return "";
  }
  if (typeof content === "string") {
    return content;
  }

  return content
    .filter(
      (part): part is ContentPart & { type: "text" } => part.type === "text",
    )
    .map((part) => part.text || "")
    .join("\n");
}

function getFirstLine(
  content: MessageContent | ObservationContent,
): string | null {
  const text = getTextFromContent(content);
  return text?.split("\n")[0] || null;
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
        <div className="text-sm bg-muted/50 rounded border border-dashed border-muted-foreground/50 p-4">
          <div className="flex items-center gap-2 text-muted-foreground mb-2">
            <ImageOff className="h-4 w-4" />
            <span className="font-medium">Image unavailable</span>
            {error.status > 0 && (
              <span className="text-xs bg-muted px-1.5 py-0.5 rounded">
                {error.status}
              </span>
            )}
          </div>
          <div className="text-xs font-mono text-muted-foreground/80 break-all">
            {path}
          </div>
          <div className="text-xs text-muted-foreground/60 mt-2">
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
        className="max-w-full h-auto rounded border border-border"
        style={{ maxHeight: "400px" }}
        loading="lazy"
        onError={handleError}
      />
      <div className="text-xs text-muted-foreground mt-1">{path}</div>
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
      <div className="text-sm whitespace-pre-wrap break-words">
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
            <div key={idx} className="text-sm whitespace-pre-wrap break-words">
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
  const [hoverPosition, setHoverPosition] = useState<number>(0);

  if (steps.length === 0) return null;

  const startTime = steps[0].timestamp
    ? new Date(steps[0].timestamp).getTime()
    : 0;

  // Calculate durations: each step's duration is time since previous step
  const stepDurations: StepDurationInfo[] = steps.map((step, idx) => {
    const stepTime = step.timestamp ? new Date(step.timestamp).getTime() : 0;
    const prevStep = idx > 0 ? steps[idx - 1] : null;
    const prevTime = prevStep?.timestamp
      ? new Date(prevStep.timestamp).getTime()
      : stepTime;

    return {
      stepId: step.step_id,
      durationMs: Math.max(0, stepTime - prevTime),
      elapsedMs: stepTime - startTime,
    };
  });

  const totalMs = stepDurations.reduce((sum, s) => sum + s.durationMs, 0);

  if (totalMs === 0) {
    return (
      <div className="mb-4">
        <div className="h-6 bg-muted rounded" />
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
    <div className="mb-4">
      <div className="relative">
        {hoveredIndex !== null && (
          <div
            className="absolute bottom-full mb-2 z-10 -translate-x-1/2 pointer-events-none"
            style={{ left: `${hoverPosition}%` }}
          >
            <div className="bg-popover border border-border rounded-md shadow-md px-3 py-2 whitespace-nowrap text-xs">
              <div className="font-medium">
                Step #{stepDurations[hoveredIndex].stepId}
              </div>
              <div className="text-muted-foreground">
                Duration: {formatMs(stepDurations[hoveredIndex].durationMs)}
              </div>
              <div className="text-muted-foreground">
                At: {formatMs(stepDurations[hoveredIndex].elapsedMs)}
              </div>
            </div>
          </div>
        )}
        <div className="flex h-6 overflow-hidden rounded">
          {stepDurations.map((step, idx) => {
            const widthPercent = widths[idx];
            const isHovered = hoveredIndex === idx;
            const isOtherHovered =
              hoveredIndex !== null && hoveredIndex !== idx;
            const centerPosition = cumulativeWidths[idx] + widthPercent / 2;

            return (
              <div
                key={step.stepId}
                className="transition-all duration-150 cursor-pointer hover:brightness-110"
                style={{
                  width: `${widthPercent}%`,
                  backgroundColor: getOscillatingColor(idx),
                  opacity: isOtherHovered ? 0.3 : 1,
                  transform: isHovered ? "scaleY(1.1)" : "scaleY(1)",
                }}
                onMouseEnter={() => {
                  setHoveredIndex(idx);
                  setHoverPosition(centerPosition);
                }}
                onMouseLeave={() => setHoveredIndex(null)}
                onClick={() => onStepClick(idx)}
              />
            );
          })}
        </div>
      </div>
    </div>
  );
}

// =============================================================================
// Token Usage Bar Component
// =============================================================================

function TokenUsageBar({ metrics }: { metrics: FinalMetrics | null }) {
  const [hoveredSegment, setHoveredSegment] = useState<string | null>(null);

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
    <div className="mb-4">
      <div className="flex items-center gap-2 mb-1.5">
        <span className="text-[10px] text-muted-foreground uppercase tracking-wider">
          Tokens
        </span>
        <span className="text-xs text-muted-foreground">
          {total.toLocaleString()} total
        </span>
      </div>
      <div className="relative">
        {hoveredSegment && (
          <div className="absolute bottom-full mb-1 left-1/2 -translate-x-1/2 z-10 pointer-events-none">
            <div className="bg-popover border border-border rounded px-2 py-1 text-xs whitespace-nowrap shadow-md">
              {segments.find((s) => s.key === hoveredSegment)?.label}:{" "}
              {segments
                .find((s) => s.key === hoveredSegment)
                ?.value.toLocaleString()}
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
              {segment.label}: {segment.value.toLocaleString()}
            </span>
          </div>
        ))}
      </div>
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
    <div className="flex items-center gap-3 text-xs text-muted-foreground">
      {/* Mini token bar */}
      {total > 0 && (
        <div className="flex items-center gap-1.5">
          <div className="flex h-1.5 w-16 overflow-hidden rounded-full gap-px">
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
          <span className={`w-1.5 h-1.5 rounded-full ${segment.color}`} />
          {segment.value.toLocaleString()}
        </span>
      ))}
      {/* Cost */}
      {metrics.cost_usd && metrics.cost_usd > 0 && (
        <span className="text-green-500 font-medium">
          ${metrics.cost_usd.toFixed(4)}
        </span>
      )}
    </div>
  );
}

// =============================================================================
// Syntax Highlighted CodeBlock Component
// =============================================================================

// Cache the shiki highlighter promise
let shikiPromise: Promise<typeof import("shiki")> | null = null;

function getShiki() {
  if (!shikiPromise) {
    shikiPromise = import("shiki");
  }
  return shikiPromise;
}

function CodeBlock({
  code,
  language = "text",
  className,
}: {
  code: string;
  language?: "json" | "text" | "bash" | "python" | "typescript" | "javascript";
  className?: string;
}) {
  const [copied, setCopied] = useState(false);
  const [highlightedHtml, setHighlightedHtml] = useState<string | null>(null);
  const sanitizedHtml = useMemo(() => {
    if (!highlightedHtml) {
      return null;
    }
    return DOMPurify.sanitize(highlightedHtml, {
      USE_PROFILES: { html: true },
    });
  }, [highlightedHtml]);

  // Truncate very long code for performance
  const truncatedCode = useMemo(() => {
    const maxLength = 50000;
    if (code.length > maxLength) {
      return code.slice(0, maxLength) + "\n\n... (truncated)";
    }
    return code;
  }, [code]);

  useEffect(() => {
    let cancelled = false;

    async function highlight() {
      try {
        const shiki = await getShiki();
        const html = await shiki.codeToHtml(truncatedCode, {
          lang: language,
          theme: "github-dark-default",
        });
        if (!cancelled) {
          setHighlightedHtml(html);
        }
      } catch {
        // Fallback to plain text on error
        if (!cancelled) {
          setHighlightedHtml(null);
        }
      }
    }

    highlight();
    return () => {
      cancelled = true;
    };
  }, [truncatedCode, language]);

  const handleCopy = useCallback(async () => {
    await navigator.clipboard.writeText(code);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, [code]);

  return (
    <div className={`relative group ${className || ""}`}>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        onClick={handleCopy}
        className="absolute top-2 right-2 p-1.5 rounded bg-black/50 hover:bg-black/70 opacity-0 group-hover:opacity-100 transition-opacity z-10"
        title="Copy to clipboard"
      >
        {copied ? (
          <Check className="h-3 w-3 text-green-400" />
        ) : (
          <Copy className="h-3 w-3 text-gray-300" />
        )}
      </Button>
      {sanitizedHtml ? (
        <div
          className="text-xs rounded overflow-x-auto max-h-64 overflow-y-auto [&>pre]:p-3 [&>pre]:m-0 [&>pre]:bg-[#0d1117] [&>pre]:overflow-x-auto"
          dangerouslySetInnerHTML={{ __html: sanitizedHtml }}
        />
      ) : (
        <pre className="text-xs bg-[#0d1117] text-gray-300 p-3 rounded overflow-x-auto max-h-64 overflow-y-auto whitespace-pre-wrap break-words">
          {truncatedCode}
        </pre>
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
}: {
  step: TrajectoryStep;
  prevTimestamp: string | null;
  startTimestamp: string | null;
}) {
  const sourceColors: Record<string, string> = {
    system: "text-gray-500",
    user: "text-blue-500",
    agent: "text-purple-500",
  };
  const sourceLabel = step.source === "agent" ? "Agent" : step.source;

  const stepDuration = formatStepDuration(prevTimestamp, step.timestamp);
  const sinceStart = formatStepDuration(startTimestamp, step.timestamp);

  // Get first line of message for preview
  const firstLine = getFirstLine(step.message)?.slice(0, 60) || null;

  return (
    <div className="flex-1 min-w-0 flex items-center gap-3 overflow-hidden pr-2">
      <div className="flex items-center gap-2 shrink-0">
        <span className="text-xs text-muted-foreground font-mono">
          #{step.step_id}
        </span>
        <span
          className={`text-xs font-medium capitalize ${sourceColors[step.source] || "text-gray-500"}`}
        >
          {sourceLabel}
        </span>
        {step.model_name && (
          <span className="text-xs text-muted-foreground">
            {step.model_name}
          </span>
        )}
      </div>

      <span className="text-xs text-muted-foreground truncate min-w-0 flex-1">
        {firstLine || <span className="italic">No message</span>}
      </span>

      <div className="flex items-center gap-1.5 shrink-0">
        {stepDuration && (
          <Badge
            variant="secondary"
            className="text-[10px] font-normal px-1.5 py-0"
          >
            +{stepDuration}
          </Badge>
        )}
        {sinceStart && (
          <Badge
            variant="outline"
            className="text-[10px] font-normal px-1.5 py-0"
          >
            @{sinceStart}
          </Badge>
        )}
      </div>
    </div>
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
  const [expandedToolCalls, setExpandedToolCalls] = useState<Set<string>>(
    new Set(),
  );

  const toggleToolCall = (id: string) => {
    setExpandedToolCalls((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

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
          <h5 className="text-xs font-medium text-muted-foreground mb-1">
            Reasoning
          </h5>
          <div className="text-xs bg-blue-500/10 border border-blue-500/20 p-2 rounded whitespace-pre-wrap">
            {step.reasoning_content}
          </div>
        </div>
      )}

      {/* Tool Calls */}
      {step.tool_calls && step.tool_calls.length > 0 && (
        <div>
          <h5 className="text-xs font-medium text-muted-foreground mb-1">
            Tool Calls
          </h5>
          <div className="space-y-2">
            {step.tool_calls.map((tc) => {
              const isExpanded = expandedToolCalls.has(tc.tool_call_id);
              const argsStr = JSON.stringify(tc.arguments, null, 2);
              const isLongArgs = argsStr.length > 100;

              return (
                <div
                  key={tc.tool_call_id}
                  className="border border-purple-500/20 rounded overflow-hidden"
                >
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => toggleToolCall(tc.tool_call_id)}
                    className="w-full justify-start gap-2 px-2 py-1.5 bg-purple-500/10 hover:bg-purple-500/15 text-left"
                  >
                    <ChevronRight
                      className={`h-3 w-3 text-purple-500 transition-transform ${isExpanded ? "rotate-90" : ""}`}
                    />
                    <span className="text-xs font-mono text-purple-500">
                      {tc.function_name}
                    </span>
                    {!isExpanded && isLongArgs && (
                      <span className="text-[10px] text-muted-foreground">
                        (click to expand)
                      </span>
                    )}
                  </Button>
                  {(isExpanded || !isLongArgs) && (
                    <CodeBlock
                      code={argsStr}
                      language="json"
                      className="rounded-none"
                    />
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Observations */}
      {step.observation && step.observation.results.length > 0 && (
        <div>
          <h5 className="text-xs font-medium text-muted-foreground mb-1">
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
                  <CodeBlock
                    key={idx}
                    code={text || "(empty)"}
                    language="bash"
                  />
                );
              }

              return (
                <div
                  key={idx}
                  className="border border-border/60 rounded p-2 bg-muted/20"
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
        <div className="pt-2 border-t border-border/50">
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
  apiBaseUrl?: string;
}

export function TrajectoryViewer({
  trialId,
  apiBaseUrl = "/api",
}: TrajectoryViewerProps) {
  const {
    data: trajectory,
    isLoading,
    error,
  } = useSWR<Trajectory | null>(
    `${apiBaseUrl}/trials/${trialId}/trajectory`,
    fetcher,
    {
      revalidateOnFocus: false,
    },
  );

  const [expandedSteps, setExpandedSteps] = useState<string[]>([]);
  const stepRefs = useRef<(HTMLDivElement | null)[]>([]);

  // Auto-expand last step if trial has finished (helps debugging failures)
  useEffect(() => {
    if (
      trajectory &&
      trajectory.steps.length > 0 &&
      expandedSteps.length === 0
    ) {
      const lastIdx = trajectory.steps.length - 1;
      setExpandedSteps([`step-${lastIdx}`]);
    }
  }, [trajectory, expandedSteps.length]);

  const handleStepClick = (index: number) => {
    const stepKey = `step-${index}`;
    setExpandedSteps((prev) =>
      prev.includes(stepKey) ? prev : [...prev, stepKey],
    );
    // Scroll to step after a brief delay for accordion animation
    setTimeout(() => {
      stepRefs.current[index]?.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
    }, 50);
  };

  if (isLoading) {
    return (
      <div className="p-4 space-y-3">
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
        <Route className="h-8 w-8 text-red-500 mx-auto mb-2" />
        <p className="text-sm text-muted-foreground">
          Failed to load trajectory
        </p>
        <p className="text-xs text-red-500 mt-1">{error.message}</p>
      </div>
    );
  }

  if (!trajectory) {
    return (
      <div className="p-6 text-center">
        <Route className="h-10 w-10 text-muted-foreground/50 mx-auto mb-3" />
        <p className="text-sm font-medium text-muted-foreground">
          No trajectory available
        </p>
        <p className="text-xs text-muted-foreground/70 mt-1 max-w-xs mx-auto">
          This trial doesn't have ATIF trajectory data. Trajectories are
          recorded for agents that support the ATIF format.
        </p>
      </div>
    );
  }

  return (
    <div className="p-4">
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-sm font-medium flex items-center justify-between">
            <span className="flex items-center gap-2">
              <Route className="h-4 w-4" />
              Trajectory
            </span>
            <span className="text-xs font-normal text-muted-foreground">
              {trajectory.steps.length} steps
              {trajectory.final_metrics?.total_cost_usd && (
                <> · ${trajectory.final_metrics.total_cost_usd.toFixed(4)}</>
              )}
            </span>
          </CardTitle>
        </CardHeader>
        <CardContent className="pt-0 overflow-x-auto">
          {/* Token Usage Bar */}
          <TokenUsageBar metrics={trajectory.final_metrics} />

          {/* Duration Bar */}
          <StepDurationBar
            steps={trajectory.steps}
            onStepClick={handleStepClick}
          />

          {/* Steps Accordion */}
          <Accordion
            type="multiple"
            value={expandedSteps}
            onValueChange={setExpandedSteps}
          >
            {trajectory.steps.map((step, idx) => (
              <AccordionItem
                key={step.step_id}
                value={`step-${idx}`}
                ref={(el: HTMLDivElement | null) => {
                  stepRefs.current[idx] = el;
                }}
              >
                <AccordionTrigger className="hover:no-underline py-3">
                  <StepTrigger
                    step={step}
                    prevTimestamp={
                      idx > 0
                        ? (trajectory.steps[idx - 1]?.timestamp ?? null)
                        : null
                    }
                    startTimestamp={trajectory.steps[0]?.timestamp ?? null}
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
          </Accordion>
        </CardContent>
      </Card>
    </div>
  );
}

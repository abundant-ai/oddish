"use client";

import { useEffect, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import {
  Sparkles,
  ChevronRight,
  AlertCircle,
  Loader2,
  RefreshCw,
} from "lucide-react";
import { phaseColorVars } from "@/lib/trajectory-metrics";
import { segmentOwners, toSegments } from "@/lib/trajectory-segments";
import type { TrajectorySummaryResource } from "@/lib/use-trajectory-summary";

interface TrajectorySummaryProps {
  resource?: TrajectorySummaryResource;
  error?: Error;
  regenerationError?: Error;
  isLoading: boolean;
  canRegenerate: boolean;
  isStartingRegeneration: boolean;
  onRetry: () => void;
  onRegenerate: () => void;
  /**
   * Map a step_id from the summary to the array index used by the
   * accordion in TrajectoryViewer. Returns -1 if the step_id is unknown
   * (in which case the row is rendered non-clickable).
   */
  stepIdToIndex: (stepId: number) => number;
  onStepSelect: (index: number) => void;
  /**
   * Renderable step ids from the viewer, so a highlight's underline resolves
   * through the same owner map the Activity card and step groups use. Without
   * it gap-fill would be measured differently here and a highlight could wear a
   * colour no other view gives that step.
   */
  renderableIds?: ReadonlySet<number>;
}

export function TrajectorySummary({
  resource,
  error,
  regenerationError,
  isLoading,
  canRegenerate,
  isStartingRegeneration,
  onRetry,
  onRegenerate,
  stepIdToIndex,
  onStepSelect,
  renderableIds,
}: TrajectorySummaryProps) {
  // The initial stored-column GET is usually fast. Pending generation arrives
  // as a resource state instead; this timer only owns the delayed read message.
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    if (!isLoading) {
      setSlow(false);
      return;
    }
    const timer = setTimeout(() => setSlow(true), 4000);
    return () => clearTimeout(timer);
  }, [isLoading]);

  const displayedError = regenerationError ?? error;
  if (displayedError) {
    const retryRefresh =
      regenerationError != null ||
      (displayedError as { status?: number }).status === 409;
    return (
      <Card className="my-3 border-red-200">
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-sm font-medium">
            <AlertCircle className="h-4 w-4 text-red-500" />
            Summary unavailable
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          <p className="text-muted-foreground text-xs">
            {displayedError.message}
          </p>
          <Button
            size="sm"
            variant="outline"
            onClick={retryRefresh && canRegenerate ? onRegenerate : onRetry}
            disabled={isStartingRegeneration}
          >
            {isStartingRegeneration && (
              <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
            )}
            Retry
          </Button>
        </CardContent>
      </Card>
    );
  }

  const waitingForGeneration =
    resource != null &&
    resource.status !== "ready" &&
    resource.status !== "missing";

  if (isLoading || waitingForGeneration) {
    return (
      <Card className="my-3">
        <CardHeader className="flex-row items-center justify-between space-y-0 pb-2">
          <CardTitle className="flex items-center gap-2 text-sm font-medium">
            <Sparkles className="h-4 w-4" /> Summary
          </CardTitle>
          {canRegenerate && waitingForGeneration && (
            <Button size="sm" variant="outline" disabled>
              <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
              Regenerating
            </Button>
          )}
        </CardHeader>
        <CardContent className="text-muted-foreground flex items-center gap-2 text-sm">
          <Loader2 className="h-4 w-4 animate-spin" />
          {waitingForGeneration
            ? resource.status === "settling"
              ? "Publishing summary…"
              : "Generating summary…"
            : slow
              ? "Retrieving summary… (storage is taking longer than expected)"
              : "Retrieving summary…"}
        </CardContent>
      </Card>
    );
  }

  if (resource?.status === "missing") {
    if (!canRegenerate) return null;
    return (
      <Card className="my-3">
        <CardHeader className="flex-row items-center justify-between space-y-0 pb-2">
          <CardTitle className="flex items-center gap-2 text-sm font-medium">
            <Sparkles className="h-4 w-4" /> Summary
          </CardTitle>
          <Button
            size="sm"
            variant="outline"
            onClick={onRegenerate}
            disabled={isStartingRegeneration}
          >
            {isStartingRegeneration ? (
              <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
            ) : (
              <RefreshCw className="mr-1 h-3.5 w-3.5" />
            )}
            Generate
          </Button>
        </CardHeader>
      </Card>
    );
  }

  if (!resource || resource.status !== "ready") return null;
  const data = resource.summary;

  // Same segment → color assignment as the Activity card and step groups, so
  // a highlight's underline matches its component everywhere.
  const segments = toSegments(data);
  const colorFor = phaseColorVars(segments.map((s) => s.key));
  const owner = segmentOwners(segments, renderableIds);

  return (
    <Card className="my-3">
      <CardHeader className="flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="flex items-center gap-2 text-sm font-medium">
          <Sparkles className="h-4 w-4" /> Summary
        </CardTitle>
        {canRegenerate && (
          <Button
            size="sm"
            variant="outline"
            onClick={onRegenerate}
            disabled={isStartingRegeneration}
          >
            {isStartingRegeneration ? (
              <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
            ) : (
              <RefreshCw className="mr-1 h-3.5 w-3.5" />
            )}
            Regenerate
          </Button>
        )}
      </CardHeader>
      <CardContent className="space-y-3">
        {data.summary && (
          <p className="text-foreground text-sm leading-relaxed">
            {data.summary}
          </p>
        )}
        {data.highlights.length > 0 && (
          <ul className="space-y-1">
            {data.highlights.map((h) => {
              const index = stepIdToIndex(h.step_id);
              const disabled = index < 0;
              const componentKey = owner.get(Number(h.step_id))?.key;
              return (
                <li key={h.step_id}>
                  <button
                    type="button"
                    disabled={disabled}
                    onClick={() => onStepSelect(index)}
                    className="group hover:bg-muted flex w-full items-start gap-2 rounded-md p-2 text-left text-sm disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    <ChevronRight
                      aria-hidden="true"
                      className="text-muted-foreground group-hover:text-foreground mt-0.5 h-4 w-4 shrink-0"
                    />
                    <span className="flex-1">
                      {/* Unclaimed steps still get an underline, in the same
                          neutral color the timeline gives them. */}
                      <span
                        className="border-b-2 pb-px font-medium"
                        style={{
                          borderColor:
                            (componentKey
                              ? colorFor.get(componentKey)
                              : undefined) ?? "var(--phase-other)",
                        }}
                      >
                        Step {h.step_id} · {h.title}
                      </span>
                      {h.why && (
                        <span className="text-muted-foreground mt-1 block text-xs">
                          {h.why}
                        </span>
                      )}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

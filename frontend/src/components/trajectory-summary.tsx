"use client";

import { useEffect, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Sparkles, ChevronRight, AlertCircle, Loader2 } from "lucide-react";
import { phaseColorVars } from "@/lib/trajectory-metrics";
import { segmentOwners, toSegments } from "@/lib/trajectory-segments";
import { useTrajectorySummary } from "@/lib/use-trajectory-summary";

interface TrajectorySummaryProps {
  trialId: string;
  /**
   * Map a step_id from the summary to the array index used by the
   * accordion in TrajectoryViewer. Returns -1 if the step_id is unknown
   * (in which case the row is rendered non-clickable).
   */
  stepIdToIndex: (stepId: number) => number;
  onStepSelect: (index: number) => void;
  apiBaseUrl?: string;
}

export function TrajectorySummary({
  trialId,
  stepIdToIndex,
  onStepSelect,
  apiBaseUrl = "/api",
}: TrajectorySummaryProps) {
  const { data, error, isLoading, mutate } = useTrajectorySummary(trialId, apiBaseUrl);

  // A stored summary returns in well under a second; only a long in-flight
  // request means the backend is actually generating one (~30s).
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    if (!isLoading) {
      setSlow(false);
      return;
    }
    const timer = setTimeout(() => setSlow(true), 4000);
    return () => clearTimeout(timer);
  }, [isLoading]);

  if (isLoading) {
    return (
      <Card className="my-3">
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-sm font-medium">
            <Sparkles className="h-4 w-4" />
            Summary
          </CardTitle>
        </CardHeader>
        <CardContent className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          {slow ? "Generating summary… (first view can take ~30s)" : "Retrieving summary…"}
        </CardContent>
      </Card>
    );
  }

  if (error) {
    if ((error as { status?: number }).status === 404) return null;
    return (
      <Card className="my-3 border-red-200">
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-sm font-medium">
            <AlertCircle className="h-4 w-4 text-red-500" />
            Summary unavailable
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          <p className="text-xs text-muted-foreground">{error.message}</p>
          <Button size="sm" variant="outline" onClick={() => mutate()}>
            Retry
          </Button>
        </CardContent>
      </Card>
    );
  }

  if (!data) return null;

  // Same segment → color assignment as the Activity card and step groups, so
  // a highlight's underline matches its component everywhere.
  const segments = toSegments(data);
  const colorFor = phaseColorVars(segments.map((s) => s.key));
  const owner = segmentOwners(segments);

  return (
    <Card className="my-3">
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-sm font-medium">
          <Sparkles className="h-4 w-4" />
          Summary
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {data.summary && (
          <p className="text-sm leading-relaxed text-foreground">
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
                    className="group flex w-full items-start gap-2 rounded-md p-2 text-left text-sm hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    <ChevronRight aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground group-hover:text-foreground" />
                    <span className="flex-1">
                      {/* Unclaimed steps still get an underline, in the same
                          neutral color the timeline gives them. */}
                      <span
                        className="font-medium border-b-2 pb-px"
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
                        <span className="mt-1 block text-xs text-muted-foreground">
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

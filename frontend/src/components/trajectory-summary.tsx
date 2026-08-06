import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Sparkles, ChevronRight, AlertCircle, Loader2 } from "lucide-react";
import { phaseColorVars } from "@/lib/trajectory-metrics";
import { segmentOwners, toSegments } from "@/lib/trajectory-segments";
import type { TrajectorySummary as TrajectorySummaryData } from "@/lib/types";

interface TrajectorySummaryProps {
  /**
   * Map a step_id from the summary to the array index used by the
   * accordion in TrajectoryViewer. Returns -1 if the step_id is unknown
   * (in which case the row is rendered non-clickable).
   */
  stepIdToIndex: (stepId: number) => number;
  onStepSelect: (index: number) => void;
  summary: TrajectorySummaryData | null | undefined;
  isLoading: boolean;
  isPending: boolean;
  error: (Error & { status?: number }) | undefined;
  onRetry: () => void;
}

export function TrajectorySummary({
  stepIdToIndex,
  onStepSelect,
  summary,
  isLoading,
  isPending,
  error,
  onRetry,
}: TrajectorySummaryProps) {
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
          Retrieving summary…
        </CardContent>
      </Card>
    );
  }

  if (isPending) {
    return (
      <Card className="my-3">
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-sm font-medium">
            <Sparkles className="h-4 w-4" />
            Summary
          </CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          Summarizing…
        </CardContent>
      </Card>
    );
  }

  if (error) {
    if (error.status === 404) return null;
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
          <Button size="sm" variant="outline" onClick={onRetry}>
            Retry
          </Button>
        </CardContent>
      </Card>
    );
  }

  if (!summary) return null;

  // Same segment → color assignment as the Activity card and step groups, so
  // a highlight's underline matches its component everywhere.
  const segments = toSegments(summary);
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
        {summary.summary && (
          <p className="text-sm leading-relaxed text-foreground">
            {summary.summary}
          </p>
        )}
        {summary.highlights.length > 0 && (
          <ul className="space-y-1">
            {summary.highlights.map((h) => {
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

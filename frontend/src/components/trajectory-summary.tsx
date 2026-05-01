"use client";

import useSWR from "swr";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Sparkles, ChevronRight, AlertCircle, Loader2 } from "lucide-react";
import { fetcher } from "@/lib/api";
import type { TrajectorySummary as TrajectorySummaryT } from "@/lib/types";

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
  const { data, error, isLoading, mutate } = useSWR<TrajectorySummaryT | null>(
    `${apiBaseUrl}/trials/${trialId}/trajectory/summary`,
    fetcher,
    { revalidateOnFocus: false },
  );

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
          Generating summary…
        </CardContent>
      </Card>
    );
  }

  if (error) {
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
              return (
                <li key={h.step_id}>
                  <button
                    type="button"
                    disabled={disabled}
                    onClick={() => !disabled && onStepSelect(index)}
                    className="group flex w-full items-start gap-2 rounded-md p-2 text-left text-sm hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    <ChevronRight className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground group-hover:text-foreground" />
                    <span className="flex-1">
                      <span className="font-medium">
                        Step {h.step_id} · {h.title}
                      </span>
                      {h.why && (
                        <span className="block text-xs text-muted-foreground">
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

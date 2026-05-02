"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { fetcher } from "@/lib/api";
import { encodeExperimentRouteParam, formatRelativeTime } from "@/lib/utils";
import type {
  ExperimentBackfillResponse,
  ResolvedExperimentCell,
} from "@/lib/types";
import { Loader2, Play } from "lucide-react";

function formatMeanReward(value: number | null | undefined) {
  if (value == null) return "—";
  return `${Math.round(value * 100)}%`;
}

export function ExperimentCellsPanel({
  experimentId,
}: {
  experimentId: string;
}) {
  const encodedId = encodeExperimentRouteParam(experimentId);
  const {
    data: cells = [],
    error,
    mutate,
  } = useSWR<ResolvedExperimentCell[]>(
    experimentId ? `/api/experiments/${encodedId}/cells` : null,
    fetcher,
    {
      refreshInterval: 30000,
      revalidateOnFocus: false,
    }
  );
  const [isBackfilling, setIsBackfilling] = useState(false);
  const [backfillMessage, setBackfillMessage] = useState<string | null>(null);
  const [backfillError, setBackfillError] = useState<string | null>(null);

  const gapCount = useMemo(
    () =>
      cells.reduce(
        (sum, item) =>
          sum + Math.max(0, item.cell.target_n_trials - item.have_n_trials),
        0
      ),
    [cells]
  );

  async function backfillGaps() {
    setIsBackfilling(true);
    setBackfillMessage(null);
    setBackfillError(null);
    try {
      const res = await fetch(`/api/experiments/${encodedId}/backfill`, {
        method: "POST",
      });
      const data = (await res.json().catch(() => null)) as
        | ExperimentBackfillResponse
        | { detail?: string; error?: string }
        | null;
      if (!res.ok || !data || !("job_id" in data)) {
        const errorData = data && !("job_id" in data) ? data : null;
        throw new Error(
          errorData?.detail || errorData?.error || "Backfill failed"
        );
      }
      setBackfillMessage(
        data.enqueued_trials === 0
          ? "No gaps to backfill."
          : `Queued ${data.enqueued_trials} trial${data.enqueued_trials === 1 ? "" : "s"} in job ${data.job_id}.`
      );
      await mutate();
    } catch (err) {
      setBackfillError(err instanceof Error ? err.message : "Backfill failed");
    } finally {
      setIsBackfilling(false);
    }
  }

  return (
    <Card className="border-[#6f88b4]/20 shadow-xs">
      <CardHeader className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <CardTitle className="text-base">Experiment Cells</CardTitle>
          <p className="text-muted-foreground mt-1 text-xs">
            Saved task-version and agent selections resolved against pooled
            evidence.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="outline">{cells.length} cells</Badge>
          <Badge variant={gapCount > 0 ? "default" : "outline"}>
            {gapCount} gap{gapCount === 1 ? "" : "s"}
          </Badge>
          <Button
            type="button"
            size="sm"
            onClick={backfillGaps}
            disabled={isBackfilling || cells.length === 0}
          >
            {isBackfilling ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            ) : (
              <Play className="mr-2 h-4 w-4" />
            )}
            Backfill gaps
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {error ? (
          <Alert variant="destructive">
            <AlertTitle>Failed to load experiment cells</AlertTitle>
            <AlertDescription>
              The legacy task table below may still load, but this experiment is
              missing its task-first read path.
            </AlertDescription>
          </Alert>
        ) : null}
        {backfillError ? (
          <Alert variant="destructive">
            <AlertTitle>Backfill failed</AlertTitle>
            <AlertDescription>{backfillError}</AlertDescription>
          </Alert>
        ) : null}
        {backfillMessage ? (
          <Alert>
            <AlertTitle>Backfill queued</AlertTitle>
            <AlertDescription>{backfillMessage}</AlertDescription>
          </Alert>
        ) : null}

        {cells.length === 0 ? (
          <div className="bg-card/60 text-muted-foreground rounded-lg border border-dashed border-[#6f88b4]/30 px-6 py-8 text-center text-sm">
            No cells are saved for this experiment yet.{" "}
            <Link
              href="/experiments/new"
              className="text-[#5d77a5] hover:underline dark:text-[#a8b8d2]"
            >
              Build a task-first experiment
            </Link>
            .
          </div>
        ) : (
          <div className="border-border/70 overflow-x-auto rounded-lg border">
            <div className="bg-muted/40 text-muted-foreground grid min-w-[760px] grid-cols-[minmax(220px,1.5fr)_minmax(180px,1.2fr)_100px_90px_110px_130px] px-3 py-2 text-[11px] tracking-wide uppercase">
              <div>Task version</div>
              <div>Agent</div>
              <div>Have/target</div>
              <div>Gap</div>
              <div>Mean reward</div>
              <div>Last run</div>
            </div>
            {cells.map((item) => {
              const gap = Math.max(
                0,
                item.cell.target_n_trials - item.have_n_trials
              );
              const taskRouteId = item.cell.task_version_id.replace(
                /-v\d+$/,
                ""
              );
              const taskVersionNumber =
                item.cell.task_version_id.match(/-v(\d+)$/)?.[1] ?? "";
              return (
                <div
                  key={item.cell.id}
                  className="border-border/70 grid min-w-[760px] grid-cols-[minmax(220px,1.5fr)_minmax(180px,1.2fr)_100px_90px_110px_130px] items-center border-t px-3 py-3 text-sm"
                >
                  <Link
                    href={`/tasks/${encodeURIComponent(taskRouteId)}${
                      taskVersionNumber ? `?version=${taskVersionNumber}` : ""
                    }`}
                    className="truncate font-mono text-[#5d77a5] underline-offset-4 hover:underline dark:text-[#a8b8d2]"
                  >
                    {item.cell.task_version_id}
                  </Link>
                  <div className="min-w-0">
                    <div className="truncate font-mono font-medium">
                      {item.cell.harness}
                    </div>
                    <div className="text-muted-foreground truncate text-xs">
                      {item.cell.provider}/{item.cell.model}
                    </div>
                  </div>
                  <div className="font-mono">
                    {item.have_n_trials}/{item.cell.target_n_trials}
                  </div>
                  <div className="font-mono">{gap}</div>
                  <div className="font-mono">
                    {formatMeanReward(item.mean_reward)}
                  </div>
                  <div className="text-muted-foreground text-xs">
                    {item.last_run_at
                      ? formatRelativeTime(item.last_run_at)
                      : "—"}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

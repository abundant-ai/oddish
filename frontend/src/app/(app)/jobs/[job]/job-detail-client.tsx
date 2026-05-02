"use client";

import Link from "next/link";
import { useState } from "react";
import useSWR from "swr";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { TrialInspectDrawer } from "@/components/trial-inspect-drawer";
import { fetcher } from "@/lib/api";
import type { BatchJob, Trial } from "@/lib/types";
import { encodeExperimentRouteParam, formatRelativeTime } from "@/lib/utils";

const KIND_LABEL: Record<string, string> = {
  validation: "Validation",
  experiment_backfill: "Backfill",
  ad_hoc: "Ad hoc",
};

function formatReward(value: number | null | undefined) {
  if (value == null) return "—";
  return `${Math.round(value * 100)}%`;
}

function statusVariant(status: string): NonNullable<BadgeProps["variant"]> {
  if (status === "success") return "success";
  if (status === "failed") return "failed";
  if (status === "cancelled") return "failed";
  if (status === "running") return "running";
  if (status === "retrying") return "retrying";
  return "outline";
}

export function JobDetailClient({ jobId }: { jobId: string }) {
  const { data: job, error: jobError } = useSWR<BatchJob>(
    `/api/jobs/${encodeURIComponent(jobId)}`,
    fetcher,
    { refreshInterval: 15000, revalidateOnFocus: false }
  );
  const { data: trials, error: trialsError } = useSWR<Trial[]>(
    `/api/jobs/${encodeURIComponent(jobId)}/trials?limit=1000`,
    fetcher,
    { refreshInterval: 15000, revalidateOnFocus: false }
  );
  const [inspecting, setInspecting] = useState<{
    trialId: string;
    taskId: string;
  } | null>(null);

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <Button variant="ghost" size="sm" asChild className="-ml-2 h-8">
            <Link href="/jobs">Jobs</Link>
          </Button>
          <h1 className="mt-2 font-mono text-2xl font-semibold tracking-tight">
            {job?.id ?? jobId}
          </h1>
          {job ? (
            <div className="text-muted-foreground mt-2 flex flex-wrap items-center gap-2 text-xs">
              <Badge variant="outline">
                {KIND_LABEL[job.kind] ?? job.kind}
              </Badge>
              <Badge variant={statusVariant(job.status)}>{job.status}</Badge>
              <span>launched {formatRelativeTime(job.launched_at)}</span>
              {job.triggered_by_experiment_id ? (
                <Link
                  href={`/experiments/${encodeExperimentRouteParam(
                    job.triggered_by_experiment_id
                  )}`}
                  className="text-[#5d77a5] underline-offset-4 hover:underline dark:text-[#a8b8d2]"
                >
                  experiment {job.triggered_by_experiment_id}
                </Link>
              ) : null}
            </div>
          ) : null}
        </div>
      </div>

      {jobError ? (
        <div className="border-destructive/40 bg-destructive/5 rounded-md border p-3 text-sm">
          Failed to load job: {String((jobError as Error).message)}
        </div>
      ) : null}

      {job ? (
        <div className="grid gap-3 sm:grid-cols-4">
          <Card>
            <CardContent className="p-4">
              <div className="text-muted-foreground text-[10px] uppercase">
                cells
              </div>
              <div className="mt-1 font-mono text-lg">{job.cells.length}</div>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-4">
              <div className="text-muted-foreground text-[10px] uppercase">
                worker jobs
              </div>
              <div className="mt-1 font-mono text-lg">
                {job.worker_jobs_count}
              </div>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-4">
              <div className="text-muted-foreground text-[10px] uppercase">
                active
              </div>
              <div className="mt-1 font-mono text-lg">
                {job.active_worker_jobs_count}
              </div>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-4">
              <div className="text-muted-foreground text-[10px] uppercase">
                evidence rows
              </div>
              <div className="mt-1 font-mono text-lg">{job.trials_count}</div>
            </CardContent>
          </Card>
        </div>
      ) : null}

      <Card className="border-[#6f88b4]/20 shadow-xs">
        <CardHeader>
          <CardTitle className="text-base">Job Cells</CardTitle>
        </CardHeader>
        <CardContent>
          {!job ? (
            <Skeleton className="h-32 w-full" />
          ) : job.cells.length === 0 ? (
            <div className="text-muted-foreground text-sm">No cells.</div>
          ) : (
            <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
              {job.cells.map((cell) => (
                <div
                  key={cell.id}
                  className="border-border/70 bg-muted/20 rounded-lg border p-3"
                >
                  <div className="font-mono text-sm">{cell.harness}</div>
                  <div className="text-muted-foreground mt-1 truncate text-xs">
                    {cell.provider}/{cell.model}
                  </div>
                  <div className="mt-2 flex flex-wrap gap-2">
                    <Badge variant="outline">{cell.n_trials} trials</Badge>
                    <Badge variant="outline">{cell.task_version_id}</Badge>
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <Card className="border-[#6f88b4]/20 shadow-xs">
        <CardHeader>
          <CardTitle className="text-base">Terminal Evidence</CardTitle>
        </CardHeader>
        <CardContent>
          {trialsError ? (
            <div className="border-destructive/40 bg-destructive/5 rounded-md border p-3 text-sm">
              Failed to load trials: {String((trialsError as Error).message)}
            </div>
          ) : !trials ? (
            <Skeleton className="h-40 w-full" />
          ) : trials.length === 0 ? (
            <div className="bg-card/60 text-muted-foreground rounded-lg border border-dashed border-[#6f88b4]/30 px-6 py-8 text-center text-sm">
              No terminal trial evidence exists for this job yet.
            </div>
          ) : (
            <div className="border-border/70 overflow-x-auto rounded-lg border">
              <table className="min-w-full text-sm">
                <thead>
                  <tr className="bg-muted/40 text-muted-foreground text-left text-[11px] tracking-wide uppercase">
                    <th className="px-3 py-2 font-medium">Trial</th>
                    <th className="px-3 py-2 font-medium">Task</th>
                    <th className="px-3 py-2 font-medium">Agent</th>
                    <th className="px-3 py-2 font-medium">Status</th>
                    <th className="px-3 py-2 font-medium">Reward</th>
                    <th className="px-3 py-2 font-medium">Finished</th>
                    <th className="px-3 py-2 text-right font-medium">
                      Inspect
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {trials.map((trial) => (
                    <tr key={trial.id} className="border-border/70 border-t">
                      <td className="px-3 py-3 font-mono text-xs">
                        {trial.id}
                      </td>
                      <td className="px-3 py-3">
                        <Link
                          href={`/tasks/${encodeURIComponent(trial.task_id)}${
                            trial.task_version != null
                              ? `?version=${trial.task_version}`
                              : ""
                          }`}
                          className="font-mono text-xs text-[#5d77a5] underline-offset-4 hover:underline dark:text-[#a8b8d2]"
                        >
                          {trial.task_id}
                        </Link>
                      </td>
                      <td className="px-3 py-3 font-mono text-xs">
                        {trial.agent}
                        {trial.model ? ` · ${trial.model}` : ""}
                      </td>
                      <td className="px-3 py-3">
                        <Badge variant={statusVariant(trial.status)}>
                          {trial.status}
                        </Badge>
                      </td>
                      <td className="px-3 py-3 font-mono">
                        {formatReward(trial.reward)}
                      </td>
                      <td className="text-muted-foreground px-3 py-3 text-xs">
                        {trial.finished_at
                          ? formatRelativeTime(trial.finished_at)
                          : "—"}
                      </td>
                      <td className="px-3 py-3 text-right">
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          className="h-7"
                          onClick={() =>
                            setInspecting({
                              trialId: trial.id,
                              taskId: trial.task_id,
                            })
                          }
                        >
                          Inspect
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      {inspecting ? (
        <TrialInspectDrawer
          open
          onOpenChange={(open) => {
            if (!open) setInspecting(null);
          }}
          taskId={inspecting.taskId}
          trialId={inspecting.trialId}
          siblingTrialIds={(trials ?? [])
            .filter((trial) => trial.task_id === inspecting.taskId)
            .map((trial) => trial.id)}
          onTrialChange={(trialId) =>
            setInspecting((current) =>
              current ? { ...current, trialId } : current
            )
          }
        />
      ) : null}
    </div>
  );
}

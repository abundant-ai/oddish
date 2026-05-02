"use client";

import { useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { TrialInspectDrawer } from "@/components/trial-inspect-drawer";
import { fetcher } from "@/lib/api";
import type { BatchJob, Trial } from "@/lib/types";
import { encodeExperimentRouteParam } from "@/lib/utils";

const KIND_LABEL: Record<string, string> = {
  validation: "Validation",
  experiment_backfill: "Backfill",
  ad_hoc: "Ad hoc",
};

function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function statusVariant(status: string): NonNullable<BadgeProps["variant"]> {
  if (status === "success") return "secondary";
  if (status === "failed" || status === "cancelled") return "destructive";
  if (status === "running") return "default";
  return "outline";
}

export function JobDetailClient({ jobId }: { jobId: string }) {
  const { data: job, error: jobError } = useSWR<BatchJob>(
    `/api/jobs/${encodeURIComponent(jobId)}`,
    fetcher,
    { refreshInterval: 15000, revalidateOnFocus: false }
  );
  const { data: trials, error: trialsError } = useSWR<Trial[]>(
    `/api/jobs/${encodeURIComponent(jobId)}/trials?limit=500`,
    fetcher,
    { refreshInterval: 15000, revalidateOnFocus: false }
  );
  const [inspecting, setInspecting] = useState<{
    trialId: string;
    taskId: string;
  } | null>(null);

  const successful = trials?.filter(
    (trial) => trial.status === "success"
  ).length;
  const failed = trials?.filter((trial) => trial.status === "failed").length;

  return (
    <div className="mx-auto w-full max-w-(--breakpoint-2xl) space-y-4 p-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-mono text-[26px] font-semibold tracking-[-0.02em]">
            {job?.id ?? jobId}
          </h1>
          <div className="text-muted-foreground mt-1 flex flex-wrap items-center gap-2 text-xs">
            {job ? (
              <>
                <Badge variant="outline">
                  {KIND_LABEL[job.kind] ?? job.kind}
                </Badge>
                <Badge variant={statusVariant(job.status)}>{job.status}</Badge>
                <span>launched {relativeTime(job.launched_at)}</span>
                {job.finished_at ? (
                  <span>· finished {relativeTime(job.finished_at)}</span>
                ) : null}
                {job.triggered_by_experiment_id ? (
                  <Link
                    className="underline-offset-2 hover:underline"
                    href={`/experiments/${encodeExperimentRouteParam(
                      job.triggered_by_experiment_id
                    )}`}
                  >
                    · experiment {job.triggered_by_experiment_id}
                  </Link>
                ) : null}
              </>
            ) : null}
          </div>
        </div>
        <Button variant="ghost" size="sm" asChild>
          <Link href="/jobs">← All jobs</Link>
        </Button>
      </div>

      {jobError ? (
        <div className="border-destructive/40 bg-destructive/5 rounded-md border p-3 text-sm">
          Failed to load job: {String((jobError as Error).message)}
        </div>
      ) : null}

      {job ? (
        <div className="bg-card grid grid-cols-2 gap-3 rounded-md border p-4 text-sm sm:grid-cols-5">
          <div>
            <div className="text-muted-foreground text-[10px] uppercase">
              cells
            </div>
            <div className="font-mono">{job.cells.length}</div>
          </div>
          <div>
            <div className="text-muted-foreground text-[10px] uppercase">
              worker jobs
            </div>
            <div className="font-mono">{job.worker_jobs_count}</div>
          </div>
          <div>
            <div className="text-muted-foreground text-[10px] uppercase">
              active
            </div>
            <div className="font-mono text-amber-600 dark:text-amber-400">
              {job.active_worker_jobs_count}
            </div>
          </div>
          <div>
            <div className="text-muted-foreground text-[10px] uppercase">
              succeeded
            </div>
            <div className="font-mono text-emerald-700 dark:text-emerald-400">
              {successful ?? "—"}
            </div>
          </div>
          <div>
            <div className="text-muted-foreground text-[10px] uppercase">
              failed
            </div>
            <div className="font-mono text-rose-600 dark:text-rose-400">
              {failed ?? "—"}
            </div>
          </div>
        </div>
      ) : null}

      {job ? (
        <div className="rounded-md border">
          <div className="text-muted-foreground border-b px-4 py-2 text-xs font-medium">
            Job cells
          </div>
          {job.cells.length === 0 ? (
            <div className="text-muted-foreground p-4 text-sm">No cells.</div>
          ) : (
            <div className="grid gap-2 p-3 md:grid-cols-2 xl:grid-cols-3">
              {job.cells.map((cell) => (
                <div
                  key={cell.id}
                  className="bg-muted/20 rounded-md border p-3 text-sm"
                >
                  <div className="font-mono">{cell.harness}</div>
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
        </div>
      ) : null}

      <div className="rounded-md border">
        <div className="text-muted-foreground border-b px-4 py-2 text-xs font-medium">
          Trials
        </div>
        {trialsError ? (
          <div className="p-3 text-sm">
            Failed to load trials: {String((trialsError as Error).message)}
          </div>
        ) : !trials ? (
          <Skeleton className="m-3 h-32" />
        ) : trials.length === 0 ? (
          <div className="text-muted-foreground p-6 text-center text-sm">
            No terminal trial evidence exists for this job yet.
          </div>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Trial</TableHead>
                <TableHead>Task</TableHead>
                <TableHead>Agent</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Reward</TableHead>
                <TableHead>Finished</TableHead>
                <TableHead>Inspect</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {trials.map((trial) => (
                <TableRow key={trial.id}>
                  <TableCell className="font-mono text-[11px]">
                    {trial.id}
                  </TableCell>
                  <TableCell>
                    <Link
                      className="font-mono text-xs underline-offset-2 hover:underline"
                      href={`/tasks/${encodeURIComponent(trial.task_id)}${
                        trial.task_version != null
                          ? `?version=${trial.task_version}`
                          : ""
                      }`}
                    >
                      {trial.task_id}
                    </Link>
                  </TableCell>
                  <TableCell className="font-mono text-xs">
                    {trial.agent}
                    {trial.model ? ` · ${trial.model}` : ""}
                  </TableCell>
                  <TableCell>
                    <Badge variant={statusVariant(trial.status)}>
                      {trial.status}
                    </Badge>
                  </TableCell>
                  <TableCell className="font-mono text-xs">
                    {trial.reward === null ? "—" : trial.reward.toFixed(2)}
                  </TableCell>
                  <TableCell className="text-muted-foreground text-xs">
                    {trial.finished_at
                      ? new Date(trial.finished_at).toLocaleString()
                      : "—"}
                  </TableCell>
                  <TableCell className="text-[11px] whitespace-nowrap">
                    <Button
                      size="sm"
                      variant="outline"
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
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>

      {inspecting ? (
        <TrialInspectDrawer
          open
          onOpenChange={(nextOpen) => {
            if (!nextOpen) setInspecting(null);
          }}
          trialId={inspecting.trialId}
          taskId={inspecting.taskId}
          siblingTrialIds={(trials ?? [])
            .filter((trial) => trial.task_id === inspecting.taskId)
            .map((trial) => trial.id)}
          onTrialChange={(nextId) =>
            setInspecting({ trialId: nextId, taskId: inspecting.taskId })
          }
        />
      ) : null}
    </div>
  );
}

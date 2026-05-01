"use client";

import useSWR from "swr";
import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import { fetcher } from "@/lib/api";
import { encodeExperimentRouteParam } from "@/lib/utils";
import type { JobSummary } from "@/lib/types";

interface JobTrial {
  id: string;
  task_id: string;
  task_version_id: string | null;
  status: string;
  reward: number | null;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string | null;
  agent: string;
  model: string | null;
  provider: string;
}

const KIND_LABEL: Record<string, string> = {
  validation: "Validation",
  experiment_backfill: "Backfill",
  ad_hoc: "Ad hoc",
};

function rel(iso: string | null): string {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function statusVariant(
  status: string,
): "default" | "secondary" | "outline" | "destructive" {
  if (status === "success") return "secondary";
  if (status === "failed") return "destructive";
  if (status === "running") return "default";
  return "outline";
}

export function JobDetailClient({ jobId }: { jobId: string }) {
  const { data: job, error: jobError } = useSWR<JobSummary>(
    `/api/jobs/${encodeURIComponent(jobId)}`,
    fetcher,
    { refreshInterval: 15_000 },
  );
  const { data: trials, error: trialsError } = useSWR<JobTrial[]>(
    `/api/jobs/${encodeURIComponent(jobId)}/trials?limit=500`,
    fetcher,
    { refreshInterval: 15_000 },
  );

  return (
    <div className="mx-auto w-full max-w-(--breakpoint-2xl) space-y-4 p-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-mono text-[26px] font-semibold tracking-[-0.02em]">
            {job?.name ?? jobId}
          </h1>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            {job ? (
              <>
                <Badge variant="outline">
                  {KIND_LABEL[job.kind] ?? job.kind}
                </Badge>
                <span>launched {rel(job.launched_at)}</span>
                {job.finished_at ? (
                  <span>· finished {rel(job.finished_at)}</span>
                ) : null}
                {job.triggered_by_experiment_id ? (
                  <Link
                    className="underline-offset-2 hover:underline"
                    href={`/experiments/${encodeExperimentRouteParam(job.triggered_by_experiment_id)}`}
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
        <div className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm">
          Failed to load job: {String((jobError as Error).message)}
        </div>
      ) : null}

      {job ? (
        <div className="grid grid-cols-2 gap-3 rounded-md border bg-card p-4 text-sm sm:grid-cols-4">
          <div>
            <div className="text-[10px] uppercase text-muted-foreground">
              total
            </div>
            <div className="font-mono">{job.trial_count}</div>
          </div>
          <div>
            <div className="text-[10px] uppercase text-muted-foreground">
              succeeded
            </div>
            <div className="font-mono text-emerald-700 dark:text-emerald-400">
              {job.succeeded_trial_count}
            </div>
          </div>
          <div>
            <div className="text-[10px] uppercase text-muted-foreground">
              failed
            </div>
            <div className="font-mono text-rose-600 dark:text-rose-400">
              {job.failed_trial_count}
            </div>
          </div>
          <div>
            <div className="text-[10px] uppercase text-muted-foreground">
              running
            </div>
            <div className="font-mono text-amber-600 dark:text-amber-400">
              {job.running_trial_count}
            </div>
          </div>
        </div>
      ) : null}

      <div className="rounded-md border">
        <div className="border-b px-4 py-2 text-xs font-medium text-muted-foreground">
          Trials
        </div>
        {trialsError ? (
          <div className="p-3 text-sm">
            Failed to load trials: {String((trialsError as Error).message)}
          </div>
        ) : !trials ? (
          <Skeleton className="m-3 h-32" />
        ) : trials.length === 0 ? (
          <div className="p-6 text-center text-sm text-muted-foreground">
            No trials produced by this job.
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
              </TableRow>
            </TableHeader>
            <TableBody>
              {trials.map((t) => (
                <TableRow key={t.id}>
                  <TableCell className="font-mono text-[11px]">
                    {t.id}
                  </TableCell>
                  <TableCell>
                    <Link
                      className="font-mono text-xs underline-offset-2 hover:underline"
                      href={`/tasks/${encodeURIComponent(t.task_id)}`}
                    >
                      {t.task_id}
                    </Link>
                  </TableCell>
                  <TableCell className="font-mono text-xs">
                    {t.agent}
                    {t.model ? ` · ${t.model}` : ""}
                  </TableCell>
                  <TableCell>
                    <Badge variant={statusVariant(t.status)}>{t.status}</Badge>
                  </TableCell>
                  <TableCell className="font-mono text-xs">
                    {t.reward === null ? "—" : t.reward.toFixed(2)}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {t.finished_at
                      ? new Date(t.finished_at).toLocaleString()
                      : "—"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>
    </div>
  );
}

"use client";

import { useMemo, useState } from "react";
import useSWR from "swr";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
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
import type { JobListResponse, JobSummary } from "@/lib/types";
import { encodeExperimentRouteParam } from "@/lib/utils";

const PAGE_SIZE = 50;

const KIND_LABEL: Record<string, string> = {
  validation: "Validation",
  experiment_backfill: "Backfill",
  ad_hoc: "Ad hoc",
};

function jobStatus(job: JobSummary): {
  label: string;
  variant: "default" | "secondary" | "outline" | "destructive";
} {
  if (job.running_trial_count > 0) return { label: "Running", variant: "default" };
  if (job.failed_trial_count > 0 && job.running_trial_count === 0) {
    if (job.succeeded_trial_count + job.failed_trial_count >= job.trial_count) {
      return { label: "Failed", variant: "destructive" };
    }
  }
  if (
    job.trial_count > 0 &&
    job.succeeded_trial_count === job.trial_count
  ) {
    return { label: "Succeeded", variant: "secondary" };
  }
  if (job.trial_count === 0) {
    return { label: "Empty", variant: "outline" };
  }
  return { label: "Queued", variant: "outline" };
}

function relTime(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  const ms = Date.now() - date.getTime();
  const sec = Math.floor(ms / 1000);
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  return `${day}d ago`;
}

export function JobsPageClient() {
  const [kind, setKind] = useState<string>("all");
  const [offset, setOffset] = useState<number>(0);

  const url = useMemo(() => {
    const params = new URLSearchParams();
    params.set("limit", String(PAGE_SIZE));
    params.set("offset", String(offset));
    if (kind !== "all") params.set("kind", kind);
    return `/api/jobs?${params.toString()}`;
  }, [kind, offset]);

  const { data, error, isLoading } = useSWR<JobListResponse>(url, fetcher, {
    refreshInterval: 30_000,
    revalidateOnFocus: false,
  });

  const onKindChange = (v: string) => {
    setKind(v);
    setOffset(0);
  };

  const jobs = data?.items ?? [];

  return (
    <div className="mx-auto w-full max-w-(--breakpoint-2xl) space-y-4 p-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-mono text-[26px] font-semibold tracking-[-0.02em]">
            Jobs
          </h1>
          <p className="text-sm text-muted-foreground">
            Recently launched batches of trials. Each Job groups the trials
            produced by one submission.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Select value={kind} onValueChange={onKindChange}>
            <SelectTrigger className="h-9 w-[180px]">
              <SelectValue placeholder="Filter kind" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All kinds</SelectItem>
              <SelectItem value="ad_hoc">Ad hoc</SelectItem>
              <SelectItem value="experiment_backfill">Backfill</SelectItem>
              <SelectItem value="validation">Validation</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>

      {error ? (
        <div className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm">
          Failed to load jobs: {String((error as Error).message)}
        </div>
      ) : null}

      {isLoading && !data ? (
        <Skeleton className="h-64 w-full" />
      ) : (
        <div className="overflow-x-auto rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Job</TableHead>
                <TableHead>Kind</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Trials</TableHead>
                <TableHead>Experiment</TableHead>
                <TableHead>Launched</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {jobs.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={6} className="text-center text-sm text-muted-foreground">
                    No jobs yet.
                  </TableCell>
                </TableRow>
              ) : (
                jobs.map((job) => {
                  const status = jobStatus(job);
                  return (
                    <TableRow key={job.id}>
                      <TableCell className="font-mono text-xs">
                        <Link
                          href={`/jobs/${encodeURIComponent(job.id)}`}
                          className="underline-offset-2 hover:underline"
                        >
                          {job.name ?? job.id}
                        </Link>
                      </TableCell>
                      <TableCell>
                        <Badge variant="outline">
                          {KIND_LABEL[job.kind] ?? job.kind}
                        </Badge>
                      </TableCell>
                      <TableCell>
                        <Badge variant={status.variant}>{status.label}</Badge>
                      </TableCell>
                      <TableCell className="font-mono text-xs">
                        <span className="text-emerald-700 dark:text-emerald-400">
                          {job.succeeded_trial_count}
                        </span>
                        /
                        <span>{job.trial_count}</span>
                        {job.failed_trial_count > 0 ? (
                          <span className="ml-1 text-rose-600 dark:text-rose-400">
                            ({job.failed_trial_count} failed)
                          </span>
                        ) : null}
                        {job.running_trial_count > 0 ? (
                          <span className="ml-1 text-amber-600 dark:text-amber-400">
                            ({job.running_trial_count} running)
                          </span>
                        ) : null}
                      </TableCell>
                      <TableCell>
                        {job.triggered_by_experiment_id ? (
                          <Link
                            className="font-mono text-xs underline-offset-2 hover:underline"
                            href={`/experiments/${encodeExperimentRouteParam(job.triggered_by_experiment_id)}`}
                          >
                            {job.triggered_by_experiment_id}
                          </Link>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        {relTime(job.launched_at)}
                      </TableCell>
                    </TableRow>
                  );
                })
              )}
            </TableBody>
          </Table>
        </div>
      )}

      {data ? (
        <div className="flex items-center justify-end gap-2 text-xs text-muted-foreground">
          <span>
            {offset + 1}–{offset + jobs.length}
          </span>
          <Button
            size="sm"
            variant="outline"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
          >
            Prev
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={!data.has_more}
            onClick={() => setOffset(offset + PAGE_SIZE)}
          >
            Next
          </Button>
        </div>
      ) : null}
    </div>
  );
}

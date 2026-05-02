"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { fetcher } from "@/lib/api";
import type { BatchJob } from "@/lib/types";
import { encodeExperimentRouteParam } from "@/lib/utils";

const KIND_LABEL: Record<string, string> = {
  validation: "Validation",
  experiment_backfill: "Backfill",
  ad_hoc: "Ad hoc",
};

function statusVariant(status: string): NonNullable<BadgeProps["variant"]> {
  if (status === "success") return "secondary";
  if (status === "failed" || status === "cancelled") return "destructive";
  if (status === "running") return "default";
  return "outline";
}

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

export function JobsClient() {
  const [kind, setKind] = useState("all");
  const { data, error, isLoading } = useSWR<BatchJob[]>(
    "/api/jobs?limit=100",
    fetcher,
    { refreshInterval: 30000, revalidateOnFocus: false }
  );

  const jobs = useMemo(() => {
    const rows = data ?? [];
    if (kind === "all") return rows;
    return rows.filter((job) => job.kind === kind);
  }, [data, kind]);

  return (
    <div className="mx-auto w-full max-w-(--breakpoint-2xl) space-y-4 p-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-mono text-[26px] font-semibold tracking-[-0.02em]">
            Jobs
          </h1>
          <p className="text-muted-foreground text-sm">
            Recently launched batches. A Job groups the worker_jobs and terminal
            trial evidence produced by one submission.
          </p>
        </div>
        <Select value={kind} onValueChange={setKind}>
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

      {error ? (
        <div className="border-destructive/40 bg-destructive/5 rounded-md border p-3 text-sm">
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
                <TableHead>Cells</TableHead>
                <TableHead>Trials</TableHead>
                <TableHead>Experiment</TableHead>
                <TableHead>Launched</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {jobs.length === 0 ? (
                <TableRow>
                  <TableCell
                    colSpan={7}
                    className="text-muted-foreground text-center text-sm"
                  >
                    No jobs match this view.
                  </TableCell>
                </TableRow>
              ) : (
                jobs.map((job) => (
                  <TableRow key={job.id}>
                    <TableCell className="font-mono text-xs">
                      <Link
                        href={`/jobs/${encodeURIComponent(job.id)}`}
                        className="underline-offset-2 hover:underline"
                      >
                        {job.id}
                      </Link>
                    </TableCell>
                    <TableCell>
                      <Badge variant="outline">
                        {KIND_LABEL[job.kind] ?? job.kind}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <Badge variant={statusVariant(job.status)}>
                        {job.status}
                      </Badge>
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      {job.cells.length}
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      {job.trials_count}
                      {job.active_worker_jobs_count > 0 ? (
                        <span className="ml-1 text-amber-600 dark:text-amber-400">
                          ({job.active_worker_jobs_count} active)
                        </span>
                      ) : null}
                    </TableCell>
                    <TableCell>
                      {job.triggered_by_experiment_id ? (
                        <Link
                          className="font-mono text-xs underline-offset-2 hover:underline"
                          href={`/experiments/${encodeExperimentRouteParam(
                            job.triggered_by_experiment_id
                          )}`}
                        >
                          {job.triggered_by_experiment_id}
                        </Link>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </TableCell>
                    <TableCell className="text-muted-foreground text-xs">
                      {relativeTime(job.launched_at)}
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </div>
      )}

      {data ? (
        <div className="text-muted-foreground flex items-center justify-end gap-2 text-xs">
          <span>{jobs.length} shown</span>
          <Button size="sm" variant="outline" disabled>
            Prev
          </Button>
          <Button size="sm" variant="outline" disabled>
            Next
          </Button>
        </div>
      ) : null}
    </div>
  );
}

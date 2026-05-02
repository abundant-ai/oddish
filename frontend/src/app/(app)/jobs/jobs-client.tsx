"use client";

import Link from "next/link";
import useSWR from "swr";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { fetcher } from "@/lib/api";
import type { BatchJob } from "@/lib/types";
import { encodeExperimentRouteParam, formatRelativeTime } from "@/lib/utils";

const KIND_LABEL: Record<string, string> = {
  validation: "Validation",
  experiment_backfill: "Backfill",
  ad_hoc: "Ad hoc",
};

function statusVariant(status: string): NonNullable<BadgeProps["variant"]> {
  if (status === "success") return "success";
  if (status === "failed") return "failed";
  if (status === "cancelled") return "failed";
  if (status === "running") return "running";
  return "outline";
}

export function JobsClient() {
  const { data, error, isLoading } = useSWR<BatchJob[]>(
    "/api/jobs?limit=100",
    fetcher,
    { refreshInterval: 15000, revalidateOnFocus: false }
  );

  return (
    <div className="space-y-5">
      <div>
        <h1 className="font-mono text-2xl font-semibold tracking-tight">
          Jobs
        </h1>
        <p className="text-muted-foreground mt-1 max-w-3xl text-sm">
          User-visible execution batches. Worker jobs remain the low-level
          queue; this page is the read-side view for submitted work.
        </p>
      </div>

      <Card className="border-[#6f88b4]/20 shadow-xs">
        <CardHeader>
          <CardTitle className="text-base">Recent Jobs</CardTitle>
        </CardHeader>
        <CardContent>
          {error ? (
            <div className="border-destructive/40 bg-destructive/5 rounded-md border p-3 text-sm">
              Failed to load jobs: {String((error as Error).message)}
            </div>
          ) : isLoading && !data ? (
            <Skeleton className="h-64 w-full" />
          ) : !data || data.length === 0 ? (
            <div className="bg-card/60 text-muted-foreground rounded-lg border border-dashed border-[#6f88b4]/30 px-6 py-10 text-center text-sm">
              No jobs have been launched yet.
            </div>
          ) : (
            <div className="border-border/70 overflow-x-auto rounded-lg border">
              <table className="min-w-full text-sm">
                <thead>
                  <tr className="bg-muted/40 text-muted-foreground text-left text-[11px] tracking-wide uppercase">
                    <th className="px-3 py-2 font-medium">Job</th>
                    <th className="px-3 py-2 font-medium">Kind</th>
                    <th className="px-3 py-2 font-medium">Status</th>
                    <th className="px-3 py-2 font-medium">Cells</th>
                    <th className="px-3 py-2 font-medium">Trials</th>
                    <th className="px-3 py-2 font-medium">Experiment</th>
                    <th className="px-3 py-2 font-medium">Launched</th>
                  </tr>
                </thead>
                <tbody>
                  {data.map((job) => (
                    <tr key={job.id} className="border-border/70 border-t">
                      <td className="px-3 py-3 font-mono text-xs">
                        <Link
                          href={`/jobs/${encodeURIComponent(job.id)}`}
                          className="text-[#5d77a5] underline-offset-4 hover:underline dark:text-[#a8b8d2]"
                        >
                          {job.id}
                        </Link>
                      </td>
                      <td className="px-3 py-3">
                        <Badge variant="outline">
                          {KIND_LABEL[job.kind] ?? job.kind}
                        </Badge>
                      </td>
                      <td className="px-3 py-3">
                        <Badge variant={statusVariant(job.status)}>
                          {job.status}
                        </Badge>
                      </td>
                      <td className="px-3 py-3 font-mono">
                        {job.cells.length}
                      </td>
                      <td className="px-3 py-3 font-mono">
                        {job.trials_count}
                        {job.active_worker_jobs_count > 0 ? (
                          <span className="ml-1 text-amber-500">
                            ({job.active_worker_jobs_count} active)
                          </span>
                        ) : null}
                      </td>
                      <td className="px-3 py-3">
                        {job.triggered_by_experiment_id ? (
                          <Link
                            href={`/experiments/${encodeExperimentRouteParam(
                              job.triggered_by_experiment_id
                            )}`}
                            className="font-mono text-xs text-[#5d77a5] underline-offset-4 hover:underline dark:text-[#a8b8d2]"
                          >
                            {job.triggered_by_experiment_id}
                          </Link>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </td>
                      <td className="text-muted-foreground px-3 py-3 text-xs">
                        {formatRelativeTime(job.launched_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

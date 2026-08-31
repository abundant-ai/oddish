"use client";

import Link from "next/link";
import { useState } from "react";
import useSWR from "swr";
import {
  Activity,
  AlertCircle,
  Clock3,
  RefreshCw,
  Workflow,
} from "lucide-react";

import { QueueKeyIcon } from "@/components/queue-key-icon";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { fetcher } from "@/lib/api";
import type {
  WorkerJobKind,
  WorkerJobSample,
  WorkerJobsResponse,
} from "@/lib/types";
import { encodeExperimentRouteParam } from "@/lib/utils";

const KIND_LABELS: Record<string, string> = {
  agent: "Agent trial",
  qa: "Task QA",
  qa_eval: "QA evaluation",
  audit: "Pre-trial audit",
  summarize: "Trajectory summary",
  task_expand: "Task expansion",
  tag_project: "Tag projection",
};

function formatAge(value: string | null): string {
  if (!value) return "no heartbeat";
  const seconds = Math.max(
    0,
    Math.floor((Date.now() - new Date(value).getTime()) / 1000)
  );
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function formatDelay(value: string): string {
  const seconds = Math.max(
    0,
    Math.ceil((new Date(value).getTime() - Date.now()) / 1000)
  );
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.ceil(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

function kindLabel(kind: WorkerJobKind): string {
  return KIND_LABELS[kind] ?? kind.replaceAll("_", " ");
}

function RunIdentityCell({ job }: { job: WorkerJobSample }) {
  return (
    <div className="min-w-44 space-y-1">
      <Badge variant="outline" className="font-mono text-[10px]">
        {kindLabel(job.kind)}
      </Badge>
      <div className="text-xs">
        {[job.agent, job.model].filter(Boolean).join(" · ") || "System work"}
      </div>
      {job.trial_id && (
        <div className="text-muted-foreground max-w-64 truncate font-mono text-[10px]">
          {job.trial_id}
        </div>
      )}
    </div>
  );
}

function JobStateCell({ job }: { job: WorkerJobSample }) {
  if (job.is_stale) {
    return (
      <div className="min-w-48 space-y-1">
        <div className="text-destructive text-xs font-medium">
          Stale · heartbeat {formatAge(job.heartbeat_at)}
        </div>
        <div className="text-muted-foreground max-w-72 truncate text-[11px]">
          {job.last_heartbeat_error ||
            job.current_worker_id ||
            "Worker stopped reporting"}
        </div>
      </div>
    );
  }

  if (job.status === "FAILED") {
    return (
      <div className="min-w-48 space-y-1">
        <div className="text-destructive text-xs font-medium">
          Failed · {formatAge(job.finished_at)}
        </div>
        <div
          className="text-muted-foreground max-w-72 truncate text-[11px]"
          title={job.error_message ?? undefined}
        >
          {job.error_message || "No error message"}
        </div>
      </div>
    );
  }

  if (job.status === "BLOCKED") {
    return (
      <div className="min-w-48 space-y-1">
        <div className="text-xs font-medium text-amber-400">Blocked</div>
        <div className="text-muted-foreground max-w-72 truncate text-[11px]">
          {job.admission_reason || "Waiting for baseline trials"}
        </div>
      </div>
    );
  }

  if (job.status === "QUEUED" || job.status === "RETRYING") {
    const scheduled = new Date(job.available_after).getTime() > Date.now();
    return (
      <div className="min-w-48 space-y-1">
        <div className="text-xs font-medium text-purple-400">
          {scheduled
            ? `${job.status === "RETRYING" ? "Retry" : "Scheduled"} in ${formatDelay(job.available_after)}`
            : `${job.status === "RETRYING" ? "Retry ready" : "Ready"} · waiting ${formatAge(job.created_at).replace(" ago", "")}`}
        </div>
        <div className="text-muted-foreground max-w-72 truncate text-[11px]">
          {job.admission_reason || "Waiting for a worker"}
        </div>
      </div>
    );
  }

  return (
    <div className="min-w-48 space-y-1">
      <div className="text-xs font-medium text-blue-400">
        Running
        {job.harbor_stage ? ` · ${job.harbor_stage.replaceAll("_", " ")}` : ""}
      </div>
      <div className="text-muted-foreground max-w-72 truncate text-[11px]">
        Heartbeat {formatAge(job.heartbeat_at)}
        {job.current_worker_id ? ` · ${job.current_worker_id}` : ""}
        {job.current_queue_slot !== null
          ? ` · slot ${job.current_queue_slot}`
          : ""}
      </div>
    </div>
  );
}

export function WorkerJobsCard() {
  const [view, setView] = useState<"active" | "attention">("active");
  const [query, setQuery] = useState("");
  const { data, error, isLoading, mutate } = useSWR<WorkerJobsResponse>(
    `/api/admin/worker-jobs?sample=${view}`,
    fetcher,
    { refreshInterval: 10000 }
  );

  const summary = data?.summary;
  const activeCount = summary
    ? summary.running + summary.ready + summary.scheduled + summary.blocked
    : 0;
  const attentionCount = summary
    ? summary.stale +
      summary.blocked +
      summary.failed_last_hour +
      (data?.pipeline_issue_count ?? 0)
    : 0;
  const needle = query.trim().toLowerCase();
  const jobs = (data?.jobs ?? []).filter((job) => {
    if (!needle) return true;
    return [
      job.kind,
      job.trial_id,
      job.task_id,
      job.task_name,
      job.experiment_id,
      job.experiment_name,
      job.agent,
      job.model,
      job.queue_key,
      job.harbor_stage,
      job.admission_reason,
      job.error_message,
    ].some((value) => value?.toLowerCase().includes(needle));
  });

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="flex items-center gap-2">
              <Workflow className="h-5 w-5" />
              <CardTitle className="text-base">Worker Jobs</CardTitle>
            </div>
            <p className="text-muted-foreground mt-1 text-xs">
              What is running, what is waiting, and what needs attention.
            </p>
          </div>
          <div className="flex items-center gap-2">
            {data && (
              <span className="text-muted-foreground text-[10px]">
                Updated {new Date(data.timestamp).toLocaleTimeString()}
              </span>
            )}
            <Button
              variant="outline"
              size="icon"
              className="h-8 w-8"
              onClick={() => mutate()}
              disabled={isLoading}
              aria-label="Refresh worker jobs"
            >
              <RefreshCw
                className={`h-4 w-4 ${isLoading ? "animate-spin" : ""}`}
              />
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-5">
        {error ? (
          <Alert variant="destructive">
            <AlertCircle className="h-4 w-4" />
            <AlertTitle>Failed to load worker jobs</AlertTitle>
            <AlertDescription>
              {error instanceof Error
                ? error.message
                : "Check your admin access."}
            </AlertDescription>
          </Alert>
        ) : !data || !summary ? (
          <p className="text-muted-foreground">Loading...</p>
        ) : (
          <>
            <div className="grid gap-3 sm:grid-cols-3">
              <div className="rounded-lg border p-3">
                <div className="text-muted-foreground flex items-center gap-1.5 text-[11px] uppercase">
                  <Activity className="h-3.5 w-3.5" /> Running
                </div>
                <div className="mt-1 text-2xl font-semibold">
                  {summary.running}
                </div>
                <div className="text-muted-foreground mt-1 text-[11px]">
                  {summary.by_kind
                    .filter((row) => row.running > 0)
                    .map(
                      (row) =>
                        `${row.running} ${kindLabel(row.kind).toLowerCase()}`
                    )
                    .join(" · ") || "No jobs running"}
                </div>
              </div>
              <div className="rounded-lg border p-3">
                <div className="text-muted-foreground flex items-center gap-1.5 text-[11px] uppercase">
                  <Clock3 className="h-3.5 w-3.5" /> Waiting
                </div>
                <div className="mt-1 text-2xl font-semibold">
                  {summary.ready + summary.scheduled + summary.blocked}
                </div>
                <div className="text-muted-foreground mt-1 text-[11px]">
                  {summary.ready} ready · {summary.scheduled} scheduled ·{" "}
                  {summary.blocked} blocked
                </div>
              </div>
              <div
                className={`rounded-lg border p-3 ${
                  attentionCount > 0 ? "border-amber-400/50" : ""
                }`}
              >
                <div className="text-muted-foreground flex items-center gap-1.5 text-[11px] uppercase">
                  <AlertCircle className="h-3.5 w-3.5" /> Needs attention
                </div>
                <div className="mt-1 text-2xl font-semibold">
                  {attentionCount}
                </div>
                <div className="text-muted-foreground mt-1 text-[11px]">
                  {summary.stale} stale · {summary.blocked} blocked ·{" "}
                  {summary.failed_last_hour} failed in 1h ·{" "}
                  {data.pipeline_issue_count} stuck tasks
                </div>
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <Button
                variant={view === "active" ? "default" : "outline"}
                size="sm"
                className="h-8"
                onClick={() => setView("active")}
              >
                Active {activeCount}
              </Button>
              <Button
                variant={view === "attention" ? "default" : "outline"}
                size="sm"
                className="h-8"
                onClick={() => setView("attention")}
              >
                Attention {attentionCount}
              </Button>
              <Input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search task, experiment, model, agent, or queue..."
                className="h-8 min-w-64 flex-1 text-xs"
              />
            </div>

            {view === "attention" && data.pipeline_issues.length > 0 && (
              <Alert>
                <AlertCircle className="h-4 w-4" />
                <AlertTitle>
                  {data.pipeline_issue_count} active task
                  {data.pipeline_issue_count === 1 ? "" : "s"} without active
                  work
                </AlertTitle>
                <AlertDescription className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
                  {data.pipeline_issues.map((issue) => (
                    <Link
                      key={issue.task_id}
                      href={`/tasks/${encodeURIComponent(issue.task_id)}`}
                      className="underline underline-offset-2"
                    >
                      {issue.task_name} · {issue.status.toLowerCase()}
                    </Link>
                  ))}
                </AlertDescription>
              </Alert>
            )}

            <div className="overflow-x-auto">
              <Table className="min-w-[980px]">
                <TableHeader>
                  <TableRow>
                    <TableHead>Run</TableHead>
                    <TableHead>Task / experiment</TableHead>
                    <TableHead>State</TableHead>
                    <TableHead>Queue</TableHead>
                    <TableHead className="text-right">Attempt</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {jobs.map((job) => (
                    <TableRow key={job.id}>
                      <TableCell>
                        <RunIdentityCell job={job} />
                      </TableCell>
                      <TableCell>
                        <div className="min-w-48 space-y-1 text-xs">
                          {job.task_id ? (
                            <Link
                              href={`/tasks/${encodeURIComponent(job.task_id)}`}
                              className="font-medium hover:underline"
                            >
                              {job.task_name || job.task_id}
                            </Link>
                          ) : (
                            <span className="text-muted-foreground">
                              No task
                            </span>
                          )}
                          {job.experiment_id && (
                            <div>
                              <Link
                                href={`/experiments/${encodeExperimentRouteParam(
                                  job.experiment_id
                                )}`}
                                className="text-muted-foreground hover:underline"
                              >
                                {job.experiment_name || job.experiment_id}
                              </Link>
                            </div>
                          )}
                        </div>
                      </TableCell>
                      <TableCell>
                        <JobStateCell job={job} />
                      </TableCell>
                      <TableCell>
                        <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
                          <QueueKeyIcon queueKey={job.queue_key} size={12} />
                          <span className="font-mono text-[11px]">
                            {job.queue_key}
                          </span>
                        </span>
                      </TableCell>
                      <TableCell className="text-right font-mono text-[11px]">
                        {job.attempts}/{job.max_attempts}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>

            {jobs.length === 0 && (
              <p className="text-muted-foreground py-5 text-center text-sm">
                {query
                  ? "No jobs match this search."
                  : view === "active"
                    ? "No active jobs."
                    : "Nothing needs attention."}
              </p>
            )}
            {data.truncated && (
              <p className="text-muted-foreground text-xs">
                Showing the {data.jobs.length} highest-priority jobs of{" "}
                {data.total_jobs}; exact totals above include every job.
              </p>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

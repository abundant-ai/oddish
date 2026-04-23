"use client";

import useSWR from "swr";
import { useMemo, useState } from "react";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Alert, AlertTitle, AlertDescription } from "@/components/ui/alert";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import type {
  WorkerJobKind,
  WorkerJobSample,
  WorkerJobStatus,
  WorkerJobsResponse,
} from "@/lib/types";
import { fetcher } from "@/lib/api";
import { QueueKeyIcon } from "@/components/queue-key-icon";
import {
  AlertCircle,
  Beaker,
  Gavel,
  Microscope,
  RefreshCw,
  Workflow,
} from "lucide-react";

// ---------------------------------------------------------------------------
// Kinds & status — display config
//
// Kept as simple maps rather than an enum so new kinds (QA_REVIEW,
// future) show up automatically with a sensible default treatment.
// ---------------------------------------------------------------------------

const KIND_ORDER: WorkerJobKind[] = [
  "TRIAL",
  "ANALYSIS",
  "VERDICT",
  "QA_REVIEW",
];

const KIND_DISPLAY: Record<
  string,
  {
    label: string;
    description: string;
    Icon: typeof Beaker;
    accent: string;
  }
> = {
  TRIAL: {
    label: "Trials",
    description: "Harbor runs for agent × model combinations",
    Icon: Beaker,
    accent: "text-blue-400",
  },
  ANALYSIS: {
    label: "Trajectory Analysis",
    description: "LLM classification of individual trial trajectories",
    Icon: Microscope,
    accent: "text-purple-400",
  },
  VERDICT: {
    label: "Task Verdict",
    description: "Cross-trial synthesis once analyses complete",
    Icon: Gavel,
    accent: "text-amber-400",
  },
  QA_REVIEW: {
    label: "QA Review",
    description: "Follow-up agent jobs reviewing completed work",
    Icon: Workflow,
    accent: "text-teal-400",
  },
};

// Status columns displayed in the kind × status matrix. ``SUCCESS`` is
// excluded from the primary matrix because it dominates totals and
// crowds out actionable numbers; it's surfaced in an aggregate footer.
const STATUS_COLUMNS: WorkerJobStatus[] = [
  "QUEUED",
  "RUNNING",
  "RETRYING",
  "FAILED",
  "CANCELLED",
  "BLOCKED",
];

const STATUS_CELL_CLASS: Record<string, string> = {
  QUEUED: "text-purple-400",
  RUNNING: "text-blue-400",
  RETRYING: "text-amber-400",
  SUCCESS: "text-green-400",
  FAILED: "text-red-400",
  CANCELLED: "text-muted-foreground",
  BLOCKED: "text-slate-400",
};

function formatAge(dateStr: string | null): string {
  if (!dateStr) return "—";
  const diffMs = Date.now() - new Date(dateStr).getTime();
  if (diffMs <= 0) return "0s";
  const totalSeconds = Math.floor(diffMs / 1000);
  if (totalSeconds < 60) return `${totalSeconds}s`;
  if (totalSeconds < 3600) return `${Math.floor(totalSeconds / 60)}m`;
  if (totalSeconds < 86400) return `${Math.floor(totalSeconds / 3600)}h`;
  return `${Math.floor(totalSeconds / 86400)}d`;
}

function formatSeconds(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "0s";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

// ---------------------------------------------------------------------------
// Kind × Status matrix
// ---------------------------------------------------------------------------

function KindRow({
  kind,
  counts,
  total,
}: {
  kind: WorkerJobKind;
  counts: Partial<Record<WorkerJobStatus, number>>;
  total: number;
}) {
  const display = KIND_DISPLAY[kind] ?? {
    label: kind,
    description: "Custom worker-job kind",
    Icon: Workflow,
    accent: "text-muted-foreground",
  };
  const Icon = display.Icon;

  return (
    <TableRow>
      <TableCell className="min-w-[220px]">
        <div className="flex items-start gap-2">
          <Icon className={`mt-0.5 h-4 w-4 ${display.accent}`} />
          <div>
            <div className="font-medium">{display.label}</div>
            <div className="text-[11px] text-muted-foreground">
              {display.description}
            </div>
          </div>
        </div>
      </TableCell>
      {STATUS_COLUMNS.map((status) => {
        const value = counts[status] ?? 0;
        return (
          <TableCell
            key={status}
            className={`text-right font-mono text-xs ${
              value > 0 ? STATUS_CELL_CLASS[status] : "text-muted-foreground/40"
            }`}
          >
            {value}
          </TableCell>
        );
      })}
      <TableCell className="text-right font-mono text-xs">
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="cursor-help">{total}</span>
          </TooltipTrigger>
          <TooltipContent>
            Total {display.label.toLowerCase()} rows across every status
          </TooltipContent>
        </Tooltip>
      </TableCell>
    </TableRow>
  );
}

function KindStatusMatrix({ data }: { data: WorkerJobsResponse }) {
  const knownKinds = new Set<string>(Object.keys(data.counts));
  const kindsToShow: WorkerJobKind[] = [
    ...KIND_ORDER.filter((k) => knownKinds.has(k)),
    ...Array.from(knownKinds).filter(
      (k) => !KIND_ORDER.includes(k as WorkerJobKind),
    ),
  ];

  if (kindsToShow.length === 0) {
    return (
      <div className="py-6 text-center text-sm text-muted-foreground">
        No worker jobs yet. Submit a task to seed the queue.
      </div>
    );
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Kind</TableHead>
          {STATUS_COLUMNS.map((status) => (
            <TableHead key={status} className="text-right text-[10px]">
              {status}
            </TableHead>
          ))}
          <TableHead className="text-right text-[10px]">Total</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {kindsToShow.map((kind) => {
          const row = data.counts[kind] ?? {};
          const total = Object.values(row).reduce<number>(
            (sum, value) => sum + (value ?? 0),
            0,
          );
          return <KindRow key={kind} kind={kind} counts={row} total={total} />;
        })}
      </TableBody>
    </Table>
  );
}

// ---------------------------------------------------------------------------
// Sample tables (stale RUNNING + recent failures)
// ---------------------------------------------------------------------------

function KindBadge({ kind }: { kind: WorkerJobKind }) {
  const display = KIND_DISPLAY[kind];
  const Icon = display?.Icon ?? Workflow;
  const accent = display?.accent ?? "text-muted-foreground";
  return (
    <Badge
      variant="outline"
      className={`gap-1 font-mono text-[10px] ${accent}`}
    >
      <Icon className="h-3 w-3" />
      {kind}
    </Badge>
  );
}

function SubjectCell({ sample }: { sample: WorkerJobSample }) {
  if (!sample.subject_id) {
    return <span className="text-muted-foreground">—</span>;
  }
  return (
    <span className="font-mono text-[11px]">
      <span className="text-muted-foreground">
        {sample.subject_table ?? "?"}/
      </span>
      {sample.subject_id}
    </span>
  );
}

function StaleRunningTable({ samples }: { samples: WorkerJobSample[] }) {
  if (samples.length === 0) {
    return (
      <p className="py-3 text-xs text-muted-foreground">
        No stale RUNNING jobs. Heartbeats are current.
      </p>
    );
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Kind</TableHead>
          <TableHead>Subject</TableHead>
          <TableHead>Queue Key</TableHead>
          <TableHead className="text-right">Attempt</TableHead>
          <TableHead className="text-right">HB Age</TableHead>
          <TableHead>HB Errors</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {samples.map((sample) => (
          <TableRow key={sample.id}>
            <TableCell>
              <KindBadge kind={sample.kind} />
            </TableCell>
            <TableCell>
              <SubjectCell sample={sample} />
            </TableCell>
            <TableCell>
              <span className="inline-flex items-center gap-1.5">
                <QueueKeyIcon queueKey={sample.queue_key} size={12} />
                <span className="font-mono text-[11px]">
                  {sample.queue_key}
                </span>
              </span>
            </TableCell>
            <TableCell className="text-right font-mono text-[11px]">
              {sample.attempts}/{sample.max_attempts}
            </TableCell>
            <TableCell className="text-right font-mono text-[11px] text-amber-400">
              {formatAge(sample.heartbeat_at)}
            </TableCell>
            <TableCell className="max-w-[280px] truncate text-[11px] text-muted-foreground">
              {sample.heartbeat_failure_count > 0 ? (
                <Tooltip>
                  <TooltipTrigger asChild>
                    <span className="cursor-help">
                      {sample.heartbeat_failure_count} failure
                      {sample.heartbeat_failure_count === 1 ? "" : "s"}
                      {sample.last_heartbeat_error
                        ? ` · ${sample.last_heartbeat_error}`
                        : ""}
                    </span>
                  </TooltipTrigger>
                  {sample.last_heartbeat_error ? (
                    <TooltipContent className="max-w-[480px]">
                      {sample.last_heartbeat_error}
                    </TooltipContent>
                  ) : null}
                </Tooltip>
              ) : (
                "—"
              )}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

function RecentFailuresTable({ samples }: { samples: WorkerJobSample[] }) {
  if (samples.length === 0) {
    return (
      <p className="py-3 text-xs text-muted-foreground">
        No recent failures or cancellations.
      </p>
    );
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Kind</TableHead>
          <TableHead>Status</TableHead>
          <TableHead>Subject</TableHead>
          <TableHead>Queue Key</TableHead>
          <TableHead className="text-right">Attempt</TableHead>
          <TableHead className="text-right">Age</TableHead>
          <TableHead>Error</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {samples.map((sample) => (
          <TableRow key={sample.id}>
            <TableCell>
              <KindBadge kind={sample.kind} />
            </TableCell>
            <TableCell>
              <span
                className={`font-mono text-[10px] ${
                  STATUS_CELL_CLASS[sample.status] ?? "text-muted-foreground"
                }`}
              >
                {sample.status}
              </span>
            </TableCell>
            <TableCell>
              <SubjectCell sample={sample} />
            </TableCell>
            <TableCell>
              <span className="inline-flex items-center gap-1.5">
                <QueueKeyIcon queueKey={sample.queue_key} size={12} />
                <span className="font-mono text-[11px]">
                  {sample.queue_key}
                </span>
              </span>
            </TableCell>
            <TableCell className="text-right font-mono text-[11px]">
              {sample.attempts}/{sample.max_attempts}
            </TableCell>
            <TableCell className="text-right font-mono text-[11px] text-muted-foreground">
              {formatAge(sample.finished_at)}
            </TableCell>
            <TableCell className="max-w-[360px] truncate text-[11px] text-muted-foreground">
              {sample.error_message ? (
                <Tooltip>
                  <TooltipTrigger asChild>
                    <span className="cursor-help">{sample.error_message}</span>
                  </TooltipTrigger>
                  <TooltipContent className="max-w-[520px]">
                    {sample.error_message}
                  </TooltipContent>
                </Tooltip>
              ) : (
                "—"
              )}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

// ---------------------------------------------------------------------------
// Duration percentiles
// ---------------------------------------------------------------------------

function DurationsTable({
  durations,
}: {
  durations: WorkerJobsResponse["durations_last_hour"];
}) {
  if (durations.length === 0) {
    return (
      <p className="py-3 text-xs text-muted-foreground">
        No completed jobs in the last hour with enough samples for percentiles.
      </p>
    );
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Kind</TableHead>
          <TableHead>Queue Key</TableHead>
          <TableHead className="text-right">Samples</TableHead>
          <TableHead className="text-right">p50</TableHead>
          <TableHead className="text-right">p95</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {durations.map((row) => (
          <TableRow key={`${row.kind}-${row.queue_key}`}>
            <TableCell>
              <KindBadge kind={row.kind} />
            </TableCell>
            <TableCell>
              <span className="inline-flex items-center gap-1.5">
                <QueueKeyIcon queueKey={row.queue_key} size={12} />
                <span className="font-mono text-[11px]">{row.queue_key}</span>
              </span>
            </TableCell>
            <TableCell className="text-right font-mono text-[11px]">
              {row.sample_count}
            </TableCell>
            <TableCell className="text-right font-mono text-[11px]">
              {formatSeconds(row.p50_seconds)}
            </TableCell>
            <TableCell className="text-right font-mono text-[11px]">
              {formatSeconds(row.p95_seconds)}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

// ---------------------------------------------------------------------------
// Top-level card
// ---------------------------------------------------------------------------

export function WorkerJobsCard() {
  const [kindFilter, setKindFilter] = useState("");

  const { data, error, isLoading, mutate } = useSWR<WorkerJobsResponse>(
    "/api/admin/worker-jobs",
    fetcher,
    { refreshInterval: 10000 },
  );

  const filterNeedle = kindFilter.trim().toLowerCase();
  const filteredStale = useMemo(() => {
    if (!data) return [];
    if (!filterNeedle) return data.stale_running;
    return data.stale_running.filter(
      (sample) =>
        sample.kind.toLowerCase().includes(filterNeedle) ||
        sample.queue_key.toLowerCase().includes(filterNeedle) ||
        (sample.subject_id ?? "").toLowerCase().includes(filterNeedle),
    );
  }, [data, filterNeedle]);
  const filteredFailures = useMemo(() => {
    if (!data) return [];
    if (!filterNeedle) return data.recent_failures;
    return data.recent_failures.filter(
      (sample) =>
        sample.kind.toLowerCase().includes(filterNeedle) ||
        sample.queue_key.toLowerCase().includes(filterNeedle) ||
        (sample.subject_id ?? "").toLowerCase().includes(filterNeedle),
    );
  }, [data, filterNeedle]);
  const filteredDurations = useMemo(() => {
    if (!data) return [];
    if (!filterNeedle) return data.durations_last_hour;
    return data.durations_last_hour.filter(
      (row) =>
        row.kind.toLowerCase().includes(filterNeedle) ||
        row.queue_key.toLowerCase().includes(filterNeedle),
    );
  }, [data, filterNeedle]);

  const totalsByStatus = useMemo(() => {
    if (!data) return null;
    const totals: Record<string, number> = {};
    for (const kind of Object.keys(data.counts)) {
      const row = data.counts[kind as WorkerJobKind] ?? {};
      for (const status of Object.keys(row)) {
        totals[status] =
          (totals[status] ?? 0) + (row[status as WorkerJobStatus] ?? 0);
      }
    }
    return totals;
  }, [data]);

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <Workflow className="h-5 w-5" />
            <CardTitle className="text-base">Worker Jobs</CardTitle>
            {totalsByStatus && (
              <div className="flex flex-wrap gap-1">
                {STATUS_COLUMNS.filter(
                  (status) => (totalsByStatus[status] ?? 0) > 0,
                ).map((status) => (
                  <Badge
                    key={status}
                    variant="outline"
                    className={`text-[10px] ${STATUS_CELL_CLASS[status]}`}
                  >
                    {status.toLowerCase()} {totalsByStatus[status]}
                  </Badge>
                ))}
              </div>
            )}
          </div>
          <div className="flex items-center gap-2">
            {data && (
              <span className="text-[10px] text-muted-foreground">
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
        <p className="text-xs text-muted-foreground">
          Every trial, trajectory analysis, and task verdict runs as its own
          queued worker job. Counts below are live from the unified{" "}
          <span className="font-mono">worker_jobs</span> table.
        </p>
      </CardHeader>
      <CardContent className="space-y-5">
        {error ? (
          <Alert variant="destructive">
            <AlertCircle className="h-4 w-4" />
            <AlertTitle>Failed to load worker jobs</AlertTitle>
            <AlertDescription>
              {error instanceof Error
                ? error.message
                : "Check if you have admin access."}
            </AlertDescription>
          </Alert>
        ) : !data ? (
          <p className="text-muted-foreground">Loading...</p>
        ) : (
          <TooltipProvider delayDuration={150}>
            <Input
              value={kindFilter}
              onChange={(event) => setKindFilter(event.target.value)}
              placeholder="Filter by kind, queue key, or subject id..."
              className="h-8 text-xs"
            />

            <section className="space-y-2">
              <h3 className="text-sm font-medium">Counts by kind × status</h3>
              <KindStatusMatrix data={data} />
            </section>

            <section className="space-y-2">
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-medium">Stale RUNNING</h3>
                <span className="text-[11px] text-muted-foreground">
                  Heartbeat older than {data.stale_after_minutes} minutes
                </span>
              </div>
              <StaleRunningTable samples={filteredStale} />
            </section>

            <section className="space-y-2">
              <h3 className="text-sm font-medium">Recent failures & cancels</h3>
              <RecentFailuresTable samples={filteredFailures} />
            </section>

            <section className="space-y-2">
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-medium">
                  Duration percentiles (last hour)
                </h3>
                <span className="text-[11px] text-muted-foreground">
                  claimed_at → finished_at, grouped by kind × queue_key
                </span>
              </div>
              <DurationsTable durations={filteredDurations} />
            </section>
          </TooltipProvider>
        )}
      </CardContent>
    </Card>
  );
}

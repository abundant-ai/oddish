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
  QueueCapacityStat,
  QueueHealthResponse,
  QueueRuntimeComponentStatus,
} from "@/lib/types";
import { fetcher } from "@/lib/api";
import { QueueKeyIcon } from "@/components/queue-key-icon";
import {
  Activity,
  AlertCircle,
  CheckCircle2,
  Cpu,
  RefreshCw,
  TriangleAlert,
  Wrench,
} from "lucide-react";

function formatAgeSeconds(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)}h`;
  return `${(seconds / 86400).toFixed(1)}d`;
}

function num(payload: Record<string, unknown>, key: string): number | null {
  const value = payload[key];
  return typeof value === "number" ? value : null;
}

function bool(payload: Record<string, unknown>, key: string): boolean {
  return payload[key] === true;
}

// ---------------------------------------------------------------------------
// Component heartbeat tiles (dispatcher / reconciler)
// ---------------------------------------------------------------------------

// A heartbeat is "stale" if we haven't heard from the component in a while.
// Dispatcher polls ~180s; reconciler ~240s. Use generous multiples so a
// single skipped tick doesn't cry wolf.
const STALE_THRESHOLD_SECONDS = 600;

function DispatcherTile({
  status,
}: {
  status: QueueRuntimeComponentStatus | null;
}) {
  if (!status) {
    return (
      <HealthTile
        Icon={Cpu}
        title="Dispatcher"
        tone="muted"
        headline="No data yet"
        detail="Waiting for the first poll to report in."
      />
    );
  }
  const payload = status.payload ?? {};
  const spawned = num(payload, "spawned");
  const cap = num(payload, "max_workers_per_poll");
  const atCap = bool(payload, "spawn_cap_reached");
  const queuedTotal = num(payload, "queued_total");
  const stale = (status.age_seconds ?? Infinity) > STALE_THRESHOLD_SECONDS;
  const tone = stale ? "warn" : atCap && (queuedTotal ?? 0) > 0 ? "warn" : "ok";

  return (
    <HealthTile
      Icon={Cpu}
      title="Dispatcher"
      tone={tone}
      headline={`Last poll ${formatAgeSeconds(status.age_seconds)} ago`}
      detail={
        spawned != null
          ? `Spawned ${spawned}${cap != null ? `/${cap}` : ""} worker(s)` +
            (atCap ? " · at spawn cap" : "")
          : "—"
      }
      warning={
        stale
          ? "Dispatcher hasn't reported recently — check the Modal schedule."
          : atCap && (queuedTotal ?? 0) > 0
            ? "Spawn cap reached with backlog pending — raise MAX_WORKERS_PER_POLL."
            : undefined
      }
    />
  );
}

function ReconcilerTile({
  status,
}: {
  status: QueueRuntimeComponentStatus | null;
}) {
  if (!status) {
    return (
      <HealthTile
        Icon={Wrench}
        title="Reconciler"
        tone="muted"
        headline="No data yet"
        detail="Waiting for the first reconcile sweep to report in."
      />
    );
  }
  const payload = status.payload ?? {};
  const ok = bool(payload, "ok");
  const duration = num(payload, "duration_seconds");
  const errors = Array.isArray(payload.errors)
    ? (payload.errors as unknown[]).map(String)
    : [];
  const stale = (status.age_seconds ?? Infinity) > STALE_THRESHOLD_SECONDS;
  const tone = stale || !ok ? "warn" : "ok";

  return (
    <HealthTile
      Icon={Wrench}
      title="Reconciler"
      tone={tone}
      headline={`Last run ${formatAgeSeconds(status.age_seconds)} ago`}
      detail={
        ok
          ? `Healthy${duration != null ? ` · ${duration}s` : ""}`
          : `${errors.length} phase error(s)`
      }
      warning={
        stale
          ? "Reconciler hasn't reported recently — check the Modal schedule."
          : errors.length > 0
            ? errors.join(" · ")
            : undefined
      }
    />
  );
}

function HealthTile({
  Icon,
  title,
  tone,
  headline,
  detail,
  warning,
}: {
  Icon: typeof Cpu;
  title: string;
  tone: "ok" | "warn" | "muted";
  headline: string;
  detail: string;
  warning?: string;
}) {
  const toneClass =
    tone === "ok"
      ? "text-green-400"
      : tone === "warn"
        ? "text-amber-400"
        : "text-muted-foreground";
  const StatusIcon =
    tone === "ok" ? CheckCircle2 : tone === "warn" ? TriangleAlert : Activity;
  return (
    <div className="rounded-lg border p-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-sm font-medium">
          <Icon className="h-4 w-4" />
          {title}
        </div>
        <StatusIcon className={`h-4 w-4 ${toneClass}`} />
      </div>
      <div className="mt-1 text-sm">{headline}</div>
      <div className="text-muted-foreground text-[11px]">{detail}</div>
      {warning ? (
        <div className="mt-1 text-[11px] text-amber-400">{warning}</div>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Capacity fill
// ---------------------------------------------------------------------------

function FillBar({ stat }: { stat: QueueCapacityStat }) {
  const fill = stat.fill ?? 0;
  const pct = Math.min(Math.max(fill, 0), 1) * 100;
  // Backlog waiting while capacity sits idle is the signal that matters —
  // colour the bar amber in that case even if utilisation is "fine".
  const underfilled = stat.queued > 0 && stat.fill != null && stat.fill < 0.85;
  const barClass = underfilled
    ? "bg-amber-400"
    : fill >= 1
      ? "bg-red-400"
      : "bg-blue-400";
  return (
    <div className="flex items-center gap-2">
      <div className="bg-muted h-2 w-24 overflow-hidden rounded-full">
        <div className={`h-full ${barClass}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-muted-foreground font-mono text-[11px]">
        {stat.running}/{stat.limit || "∞"}
      </span>
    </div>
  );
}

function CapacityTable({ rows }: { rows: QueueCapacityStat[] }) {
  if (rows.length === 0) {
    return (
      <p className="text-muted-foreground py-3 text-xs">
        No active queue keys. The queue is idle.
      </p>
    );
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Queue Key</TableHead>
          <TableHead className="text-right">Queued</TableHead>
          <TableHead>Capacity (running / limit)</TableHead>
          <TableHead className="text-right">Oldest wait</TableHead>
          <TableHead className="text-right">Queue p50</TableHead>
          <TableHead className="text-right">Queue p95</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((row) => {
          const underfilled =
            row.queued > 0 && row.fill != null && row.fill < 0.85;
          return (
            <TableRow key={row.queue_key}>
              <TableCell>
                <span className="inline-flex items-center gap-1.5">
                  <QueueKeyIcon queueKey={row.queue_key} size={12} />
                  <span className="font-mono text-[11px]">{row.queue_key}</span>
                </span>
              </TableCell>
              <TableCell className="text-right font-mono text-[11px]">
                {row.queued > 0 ? (
                  <span className={underfilled ? "text-amber-400" : ""}>
                    {row.queued}
                    {row.queued_scheduled > 0 ? (
                      <span className="text-muted-foreground">
                        {" "}
                        (+{row.queued_scheduled})
                      </span>
                    ) : null}
                  </span>
                ) : (
                  <span className="text-muted-foreground/40">0</span>
                )}
              </TableCell>
              <TableCell>
                {underfilled ? (
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <span className="cursor-help">
                        <FillBar stat={row} />
                      </span>
                    </TooltipTrigger>
                    <TooltipContent>
                      {row.queued} queued but only {row.running}/{row.limit}{" "}
                      running — spare capacity is not being filled.
                    </TooltipContent>
                  </Tooltip>
                ) : (
                  <FillBar stat={row} />
                )}
              </TableCell>
              <TableCell className="text-muted-foreground text-right font-mono text-[11px]">
                {formatAgeSeconds(row.oldest_queued_age_seconds)}
              </TableCell>
              <TableCell className="text-muted-foreground text-right font-mono text-[11px]">
                {formatAgeSeconds(row.wait_p50_seconds)}
              </TableCell>
              <TableCell className="text-muted-foreground text-right font-mono text-[11px]">
                {formatAgeSeconds(row.wait_p95_seconds)}
              </TableCell>
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
  );
}

// ---------------------------------------------------------------------------
// Throughput
// ---------------------------------------------------------------------------

function ThroughputTable({
  rows,
}: {
  rows: QueueHealthResponse["throughput"];
}) {
  if (rows.length === 0) {
    return (
      <p className="text-muted-foreground py-3 text-xs">
        No jobs started or finished in the last hour.
      </p>
    );
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Kind</TableHead>
          <TableHead className="text-right">Started 5m</TableHead>
          <TableHead className="text-right">15m</TableHead>
          <TableHead className="text-right">60m</TableHead>
          <TableHead className="text-right">Finished 5m</TableHead>
          <TableHead className="text-right">15m</TableHead>
          <TableHead className="text-right">60m</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((row) => (
          <TableRow key={row.kind}>
            <TableCell className="font-mono text-[11px]">{row.kind}</TableCell>
            <TableCell className="text-right font-mono text-[11px]">
              {row.started_5m}
            </TableCell>
            <TableCell className="text-muted-foreground text-right font-mono text-[11px]">
              {row.started_15m}
            </TableCell>
            <TableCell className="text-muted-foreground text-right font-mono text-[11px]">
              {row.started_60m}
            </TableCell>
            <TableCell className="text-right font-mono text-[11px]">
              {row.finished_5m}
            </TableCell>
            <TableCell className="text-muted-foreground text-right font-mono text-[11px]">
              {row.finished_15m}
            </TableCell>
            <TableCell className="text-muted-foreground text-right font-mono text-[11px]">
              {row.finished_60m}
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

export function QueueHealthOverviewCard() {
  const [filter, setFilter] = useState("");
  const { data, error, isLoading, mutate } = useSWR<QueueHealthResponse>(
    "/api/admin/queue-health",
    fetcher,
    { refreshInterval: 10000 },
  );

  const needle = filter.trim().toLowerCase();
  const capacity = useMemo(() => {
    if (!data) return [];
    if (!needle) return data.capacity;
    return data.capacity.filter((row) =>
      row.queue_key.toLowerCase().includes(needle),
    );
  }, [data, needle]);

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <Activity className="h-5 w-5" />
            <CardTitle className="text-base">Queue Health</CardTitle>
            {data && (
              <div className="flex flex-wrap gap-1">
                <Badge
                  variant="outline"
                  className="text-[10px] text-purple-400"
                >
                  {data.totals_queued} queued
                </Badge>
                <Badge variant="outline" className="text-[10px] text-blue-400">
                  {data.totals_running} running
                </Badge>
              </div>
            )}
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
              aria-label="Refresh queue health"
            >
              <RefreshCw
                className={`h-4 w-4 ${isLoading ? "animate-spin" : ""}`}
              />
            </Button>
          </div>
        </div>
        <p className="text-muted-foreground text-xs">
          Is the queue keeping up? Throughput, per-model capacity fill, and the
          dispatcher / reconciler heartbeats that drive everything.
        </p>
      </CardHeader>
      <CardContent className="space-y-5">
        {error ? (
          <Alert variant="destructive">
            <AlertCircle className="h-4 w-4" />
            <AlertTitle>Failed to load queue health</AlertTitle>
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
            <div className="grid gap-3 sm:grid-cols-2">
              <DispatcherTile status={data.dispatcher} />
              <ReconcilerTile status={data.reconciler} />
            </div>

            <section className="space-y-2">
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-medium">Capacity fill</h3>
                <span className="text-muted-foreground text-[11px]">
                  amber = backlog with spare capacity (not being filled)
                </span>
              </div>
              <Input
                value={filter}
                onChange={(event) => setFilter(event.target.value)}
                placeholder="Filter by queue key..."
                className="h-8 text-xs"
              />
              <CapacityTable rows={capacity} />
            </section>

            <section className="space-y-2">
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-medium">Throughput</h3>
                <span className="text-muted-foreground text-[11px]">
                  jobs started / finished by kind
                </span>
              </div>
              <ThroughputTable rows={data.throughput} />
            </section>
          </TooltipProvider>
        )}
      </CardContent>
    </Card>
  );
}

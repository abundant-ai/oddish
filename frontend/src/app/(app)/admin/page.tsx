"use client";

import { useEffect, useMemo, useState } from "react";
import useSWR, { useSWRConfig } from "swr";
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import type {
  DashboardResponse,
  QueueStats,
  QueueSlotsResponse,
  QueueStatusResponse,
  OrphanedStateResponse,
  QueueSlotSummary,
} from "@/lib/types";
import { fetcher } from "@/lib/api";
import { QueueKeyIcon } from "@/components/queue-key-icon";
import {
  ChevronDown,
  RefreshCw,
  Server,
  Database,
  Clock,
  AlertCircle,
} from "lucide-react";

const formatAge = (dateStr: string | null) => {
  if (!dateStr) return "—";
  const diffMs = Date.now() - new Date(dateStr).getTime();
  if (diffMs <= 0) return "0s";
  const totalSeconds = Math.floor(diffMs / 1000);
  if (totalSeconds < 60) return `${totalSeconds}s`;
  if (totalSeconds < 3600) return `${Math.floor(totalSeconds / 60)}m`;
  if (totalSeconds < 86400) return `${Math.floor(totalSeconds / 3600)}h`;
  return `${Math.floor(totalSeconds / 86400)}d`;
};

// =============================================================================
// Queues & Pipeline (moved from dashboard)
// =============================================================================

type DashboardSample = {
  timestamp: number;
  queues: QueueStats;
};

type QueueStat = QueueStats[string];
type QueueStatKey = keyof QueueStat;

const TIME_RANGES = [
  { key: "15m", label: "15m", ms: 15 * 60 * 1000 },
  { key: "1h", label: "1h", ms: 60 * 60 * 1000 },
  { key: "6h", label: "6h", ms: 6 * 60 * 60 * 1000 },
  { key: "24h", label: "24h", ms: 24 * 60 * 60 * 1000 },
  { key: "7d", label: "7d", ms: 7 * 24 * 60 * 60 * 1000 },
] as const;

type TimeRangeKey = (typeof TIME_RANGES)[number]["key"];

const MAX_SAMPLES = 720;
function getWindowSamples(samples: DashboardSample[], rangeMs: number) {
  if (samples.length === 0) return [];
  const cutoff = Date.now() - rangeMs;
  const windowed = samples.filter((sample) => sample.timestamp >= cutoff);
  return windowed.length > 0 ? windowed : samples.slice(-1);
}

function getQueueDelta(
  windowSamples: DashboardSample[],
  queueKey: string,
  key: QueueStatKey,
) {
  if (windowSamples.length < 2) return 0;
  const first = windowSamples[0]?.queues?.[queueKey]?.[key] ?? 0;
  const last =
    windowSamples[windowSamples.length - 1]?.queues?.[queueKey]?.[key] ?? 0;
  return Math.max(0, Number(last) - Number(first));
}

function getQueueSeries(
  windowSamples: DashboardSample[],
  queueKey: string,
  selector: (stats: QueueStat | undefined) => number,
) {
  return windowSamples.map((sample) => selector(sample.queues?.[queueKey]));
}

function getQueueWindowAverage(
  windowSamples: DashboardSample[],
  queueKey: string,
  key: QueueStatKey,
) {
  if (windowSamples.length === 0) return 0;
  const total = windowSamples.reduce((sum, sample) => {
    const value = Number(sample.queues?.[queueKey]?.[key]) || 0;
    return sum + value;
  }, 0);
  return Math.round(total / windowSamples.length);
}

function getWaitingJobs(stats: QueueStat | undefined) {
  const pending = Number(stats?.pending) || 0;
  const queued = Number(stats?.queued) || 0;
  const retrying = Number(stats?.retrying) || 0;
  return pending + queued + retrying;
}

function getLiveJobs(stats: QueueStat | undefined) {
  return getWaitingJobs(stats) + (Number(stats?.running) || 0);
}

function downsampleSeries(values: number[], maxPoints: number) {
  if (values.length <= maxPoints) return values;
  const step = Math.ceil(values.length / maxPoints);
  return values.filter((_, index) => index % step === 0);
}

function formatTime(timestamp: number) {
  return new Date(timestamp).toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function MiniSparkline({
  values,
  colorClass,
}: {
  values: number[];
  colorClass: string;
}) {
  if (values.length === 0) {
    return <div className="h-6 w-full rounded bg-muted/30" />;
  }

  const compact = downsampleSeries(values, 60);
  const max = Math.max(...compact, 1);

  return (
    <div className="h-6 w-full overflow-hidden rounded bg-muted/20">
      <div className="flex h-full items-end gap-[1px]">
        {compact.map((value, index) => (
          <div
            key={`${value}-${index}`}
            className={`w-[2px] ${colorClass}`}
            style={{
              height: `${Math.max(12, (value / max) * 100)}%`,
              opacity: value === 0 ? 0.35 : 1,
            }}
          />
        ))}
      </div>
    </div>
  );
}

type BarSegment = {
  key: string;
  label: string;
  value: number;
  className: string;
  textClassName?: string;
};

function StackedBar({
  segments,
  ariaLabel,
  heightClass = "h-2",
}: {
  segments: BarSegment[];
  ariaLabel: string;
  heightClass?: string;
}) {
  const total = segments.reduce((sum, segment) => sum + segment.value, 0);

  if (total <= 0) {
    return (
      <div
        className={`w-full rounded-full bg-muted/30 ${heightClass}`}
        role="img"
        aria-label={`${ariaLabel} (empty)`}
      />
    );
  }

  return (
    <div
      className={`flex w-full overflow-hidden rounded-full bg-muted/30 ${heightClass}`}
      role="img"
      aria-label={ariaLabel}
    >
      {segments.map((segment) => {
        if (segment.value <= 0) return null;
        const width = `${(segment.value / total) * 100}%`;
        return (
          <div
            key={segment.key}
            className={segment.className}
            style={{ width }}
          />
        );
      })}
    </div>
  );
}

function SegmentLegend({ segments }: { segments: BarSegment[] }) {
  return (
    <div className="flex flex-wrap items-center gap-3 text-[10px] text-muted-foreground">
      {segments.map((segment) => (
        <span key={segment.key} className="inline-flex items-center gap-1">
          <span className={`h-2 w-2 rounded-full ${segment.className}`} />
          <span className={segment.textClassName}>{segment.label}</span>
          <span className="font-mono text-[10px]">{segment.value}</span>
        </span>
      ))}
    </div>
  );
}

function QueueKeyMatrix({
  queues,
  error,
  windowSamples,
}: {
  queues: QueueStats | null;
  error: Error | undefined;
  windowSamples: DashboardSample[];
}) {
  const [queueFilter, setQueueFilter] = useState("");
  const queueKeys = useMemo(
    () =>
      queues
        ? Object.keys(queues).filter((key) =>
            key.toLowerCase().includes(queueFilter.toLowerCase().trim()),
          )
        : [],
    [queues, queueFilter],
  );

  const rows = useMemo(() => {
    if (!queues) return [];
    return queueKeys
      .map((queueKey) => {
        const stats = queues[queueKey];
        const pending = Number(stats.pending) || 0;
        const queued = Number(stats.queued) || 0;
        const running = Number(stats.running) || 0;
        const retrying = Number(stats.retrying) || 0;
        const recommended = Number(stats.recommended_concurrency) || 0;
        const waiting = getWaitingJobs(stats);
        const liveJobs = getLiveJobs(stats);
        const mixPending = getQueueWindowAverage(
          windowSamples,
          queueKey,
          "pending",
        );
        const mixQueued = getQueueWindowAverage(
          windowSamples,
          queueKey,
          "queued",
        );
        const mixRetrying = getQueueWindowAverage(
          windowSamples,
          queueKey,
          "retrying",
        );
        const mixRunning = getQueueWindowAverage(
          windowSamples,
          queueKey,
          "running",
        );
        const deltaSuccess = getQueueDelta(windowSamples, queueKey, "success");
        const deltaFailed = getQueueDelta(windowSamples, queueKey, "failed");
        const trend = getQueueSeries(windowSamples, queueKey, (entry) => {
          const trendPending = Number(entry?.pending) || 0;
          const trendQueued = Number(entry?.queued) || 0;
          const trendRunning = Number(entry?.running) || 0;
          const trendRetrying = Number(entry?.retrying) || 0;
          return trendPending + trendQueued + trendRunning + trendRetrying;
        });

        return {
          queueKey,
          pending,
          queued,
          running,
          retrying,
          recommended,
          waiting,
          liveJobs,
          mixPending,
          mixQueued,
          mixRetrying,
          mixRunning,
          deltaSuccess,
          deltaFailed,
          trend,
        };
      })
      .sort((a, b) => b.liveJobs - a.liveJobs || b.waiting - a.waiting);
  }, [queues, queueKeys, windowSamples]);

  const totals = useMemo(() => {
    if (!queues) {
      return {
        waiting: 0,
        running: 0,
        liveJobs: 0,
      };
    }
    return Object.values(queues).reduce(
      (acc, stats) => {
        const running = Number(stats.running) || 0;
        const waiting = getWaitingJobs(stats);
        return {
          waiting: acc.waiting + waiting,
          running: acc.running + running,
          liveJobs: acc.liveJobs + waiting + running,
        };
      },
      {
        waiting: 0,
        running: 0,
        liveJobs: 0,
      },
    );
  }, [queues]);

  const lastSample = windowSamples[windowSamples.length - 1] ?? null;
  const hasQueueKeys = queueKeys.length > 0;
  const maxRows = 25;
  const visibleRows = rows.slice(0, maxRows);
  const hiddenRows = Math.max(rows.length - visibleRows.length, 0);

  return (
    <div className="flex h-[320px] flex-col">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-sm font-medium text-foreground">Worker Queues</div>
        <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
          <Badge variant="outline" className="text-[10px] font-normal">
            Queue keys {queueKeys.length}
          </Badge>
          <Badge variant="outline" className="text-[10px] font-normal">
            Waiting {totals.waiting}
          </Badge>
          <Badge variant="outline" className="text-[10px] font-normal">
            Running {totals.running}
          </Badge>
          <Badge variant="outline" className="text-[10px] font-normal">
            Live jobs {totals.liveJobs}
          </Badge>
          {hiddenRows > 0 && (
            <Badge variant="outline" className="text-[10px] font-normal">
              Showing top {visibleRows.length}/{rows.length}
            </Badge>
          )}
          {lastSample && (
            <span className="text-[10px]">
              Updated {formatTime(lastSample.timestamp)}
            </span>
          )}
        </div>
      </div>
      <div className="mt-2">
        <Input
          value={queueFilter}
          onChange={(event) => setQueueFilter(event.target.value)}
          placeholder="Filter queue keys..."
          className="h-8 text-xs"
        />
      </div>
      <div className="mt-3 flex-1 overflow-y-auto">
        {error ? (
          <Alert variant="destructive">
            <AlertTitle>Queues unavailable</AlertTitle>
            <AlertDescription>
              We could not connect to worker queues.
            </AlertDescription>
          </Alert>
        ) : !hasQueueKeys ? (
          <div className="py-6 text-center text-sm text-muted-foreground">
            No active queue keys yet.
          </div>
        ) : rows.length === 0 ? (
          <div className="py-6 text-center text-sm text-muted-foreground">
            No queue keys match the current filter.
          </div>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Queue Key</TableHead>
                <TableHead className="text-right">Capacity</TableHead>
                <TableHead>Queue Mix (avg)</TableHead>
                <TableHead className="text-right">Δ Done</TableHead>
                <TableHead className="text-right">Δ Failed</TableHead>
                <TableHead>Trend</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {visibleRows.map((row) => {
                const isOverLimit =
                  row.recommended > 0 && row.running > row.recommended;
                const queueSegments: BarSegment[] = [
                  {
                    key: "pending",
                    label: "Pending",
                    value: row.mixPending,
                    className: "bg-slate-400/80",
                    textClassName: "text-slate-400",
                  },
                  {
                    key: "queued",
                    label: "Queued",
                    value: row.mixQueued,
                    className: "bg-purple-400/80",
                    textClassName: "text-purple-400",
                  },
                  {
                    key: "retrying",
                    label: "Retrying",
                    value: row.mixRetrying,
                    className: "bg-amber-400/80",
                    textClassName: "text-amber-400",
                  },
                  {
                    key: "running",
                    label: "Running",
                    value: row.mixRunning,
                    className: "bg-blue-400/80",
                    textClassName: "text-blue-400",
                  },
                ];

                return (
                  <TableRow key={row.queueKey}>
                    <TableCell className="font-medium">
                      <span className="inline-flex items-center gap-2">
                        <QueueKeyIcon queueKey={row.queueKey} size={13} />
                        <span className="font-mono text-xs">
                          {row.queueKey}
                        </span>
                      </span>
                    </TableCell>
                    <TableCell className="text-right">
                      <Badge
                        variant={isOverLimit ? "warning" : "outline"}
                        className="text-[10px] font-normal"
                      >
                        {row.running}/{row.recommended || "—"}
                      </Badge>
                    </TableCell>
                    <TableCell className="min-w-[220px]">
                      <div className="space-y-1.5">
                        <StackedBar
                          segments={queueSegments}
                          ariaLabel={`Queue mix for ${row.queueKey}`}
                        />
                        <SegmentLegend segments={queueSegments} />
                      </div>
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs text-green-400">
                      {row.deltaSuccess}
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs text-red-400">
                      {row.deltaFailed}
                    </TableCell>
                    <TableCell className="min-w-[110px]">
                      <MiniSparkline
                        values={row.trend}
                        colorClass="bg-blue-500/70"
                      />
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        )}
        {hiddenRows > 0 && (
          <p className="mt-2 text-[11px] text-muted-foreground">
            {hiddenRows} additional queues hidden to keep this view readable.
          </p>
        )}
      </div>
    </div>
  );
}

function QueuesAndPipelineCard() {
  const { mutate } = useSWRConfig();
  const [timeRange, setTimeRange] = useState<TimeRangeKey>("1h");
  const [history, setHistory] = useState<DashboardSample[]>([]);
  const [showQueuesPipeline, setShowQueuesPipeline] = useState(false);

  const query = new URLSearchParams({
    tasks_limit: "1",
    tasks_offset: "0",
  }).toString();
  const swrKey = `/api/dashboard?${query}`;

  const { data, error } = useSWR<DashboardResponse>(swrKey, fetcher, {
    refreshInterval: 30000,
    revalidateOnFocus: false,
    keepPreviousData: true,
  });

  const queues = data?.queues ?? null;

  useEffect(() => {
    if (!queues) return;
    setHistory((prev) => {
      const timestamp = Date.now();
      const last = prev[prev.length - 1];
      if (last && timestamp - last.timestamp < 10000) {
        return prev;
      }

      const snapshot: DashboardSample = {
        timestamp,
        queues,
      };
      const next = [...prev, snapshot];
      if (next.length > MAX_SAMPLES) {
        return next.slice(next.length - MAX_SAMPLES);
      }
      return next;
    });
  }, [queues]);

  const rangeConfig =
    TIME_RANGES.find((range) => range.key === timeRange) ?? TIME_RANGES[1];
  const windowSamples = useMemo(
    () => getWindowSamples(history, rangeConfig.ms),
    [history, rangeConfig.ms],
  );

  const handleRefresh = () => {
    setHistory([]);
    mutate(
      (key) => typeof key === "string" && key.startsWith("/api/dashboard"),
    );
  };

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="text-base">Queues & Pipeline</CardTitle>
          <div className="flex flex-wrap items-center gap-1">
            <Button
              variant="outline"
              size="icon"
              className="h-8 w-8"
              onClick={handleRefresh}
              aria-label="Refresh dashboard"
            >
              <RefreshCw className="h-4 w-4" />
            </Button>
            {TIME_RANGES.map((range) => (
              <Button
                key={range.key}
                variant={timeRange === range.key ? "secondary" : "outline"}
                size="sm"
                className="h-8 px-2 text-[11px]"
                onClick={() => setTimeRange(range.key)}
              >
                {range.label}
              </Button>
            ))}
            <Button
              variant="ghost"
              size="sm"
              className="h-7 px-2 text-[11px] text-muted-foreground"
              onClick={() => setShowQueuesPipeline((prev) => !prev)}
              aria-expanded={showQueuesPipeline}
            >
              {showQueuesPipeline ? "Hide" : "Show"}
              <ChevronDown
                className={`ml-1 h-3.5 w-3.5 transition-transform ${
                  showQueuesPipeline ? "rotate-180" : ""
                }`}
              />
            </Button>
          </div>
        </div>
      </CardHeader>
      {showQueuesPipeline ? (
        <CardContent>
          <div className="rounded-md border border-muted/40 p-3">
            <QueueKeyMatrix
              queues={queues}
              error={error}
              windowSamples={windowSamples}
            />
          </div>
        </CardContent>
      ) : null}
    </Card>
  );
}

// =============================================================================
// Queue Slots Card
// =============================================================================

function QueueSlotsCard() {
  const { data, error, isLoading, mutate } = useSWR<QueueSlotsResponse>(
    `/api/admin/slots`,
    fetcher,
    {
      refreshInterval: 10000,
    },
  );

  const formatTimestamp = (ts: string | null) => {
    if (!ts) return "—";
    const date = new Date(ts);
    const now = new Date();
    const diffMs = date.getTime() - now.getTime();
    const diffSec = Math.round(diffMs / 1000);

    if (diffSec < 0) return "Expired";
    if (diffSec < 60) return `${diffSec}s`;
    if (diffSec < 3600) return `${Math.round(diffSec / 60)}m`;
    return `${Math.round(diffSec / 3600)}h`;
  };
  const [queueFilter, setQueueFilter] = useState("");
  const filteredProviders = useMemo(() => {
    if (!data?.queue_keys) return [];
    const query = queueFilter.toLowerCase().trim();
    if (!query) return data.queue_keys;
    return data.queue_keys.filter((queueSummary) => {
      const queueKey = queueSummary.queue_key;
      return queueKey.toLowerCase().includes(query);
    });
  }, [data?.queue_keys, queueFilter]);

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Server className="h-5 w-5" />
            <CardTitle className="text-base">Queue Slots</CardTitle>
            {data && (
              <Badge variant="outline" className="text-xs">
                {data.total_active}/{data.total_slots} active
              </Badge>
            )}
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={() => mutate()}
            disabled={isLoading}
          >
            <RefreshCw
              className={`h-4 w-4 ${isLoading ? "animate-spin" : ""}`}
            />
          </Button>
        </div>
        {data && (
          <p className="text-xs text-muted-foreground">
            Last updated: {new Date(data.timestamp).toLocaleTimeString()}
          </p>
        )}
      </CardHeader>
      <CardContent>
        {error ? (
          <Alert variant="destructive">
            <AlertCircle className="h-4 w-4" />
            <AlertTitle>Failed to load queue slots</AlertTitle>
            <AlertDescription>
              {error instanceof Error
                ? error.message
                : "Check if you have admin access."}
            </AlertDescription>
          </Alert>
        ) : isLoading ? (
          <p className="text-muted-foreground">Loading...</p>
        ) : !data || data.queue_keys.length === 0 ? (
          <div className="py-8 text-center text-muted-foreground">
            <Server className="mx-auto mb-3 h-12 w-12 opacity-50" />
            <p>No queue slots configured</p>
          </div>
        ) : (
          <div className="space-y-3">
            <Input
              value={queueFilter}
              onChange={(event) => setQueueFilter(event.target.value)}
              placeholder="Filter queue keys..."
              className="h-8 text-xs"
            />
            <Accordion type="multiple" className="space-y-2">
              {filteredProviders.map((queueSummary: QueueSlotSummary) => {
                const queueKey = queueSummary.queue_key;
                return (
                  <AccordionItem
                    key={queueKey}
                    value={queueKey}
                    className="rounded-lg border px-3"
                  >
                    <AccordionTrigger className="py-3 hover:no-underline">
                      <div className="flex w-full items-center justify-between pr-2">
                        <span className="font-medium">
                          <span className="inline-flex items-center gap-2">
                            <QueueKeyIcon queueKey={queueKey} size={13} />
                            <span className="font-mono text-xs">
                              {queueKey}
                            </span>
                          </span>
                        </span>
                        <div className="flex items-center gap-2">
                          <div className="flex gap-1">
                            {Array.from({
                              length: queueSummary.total_slots,
                            }).map((_, i) => (
                              <div
                                key={i}
                                className={`h-2 w-2 rounded-full ${
                                  i < queueSummary.active_slots
                                    ? "bg-blue-500"
                                    : "bg-muted-foreground/30"
                                }`}
                              />
                            ))}
                          </div>
                          <Badge
                            variant={
                              queueSummary.active_slots > 0
                                ? "default"
                                : "outline"
                            }
                            className="text-xs"
                          >
                            {queueSummary.active_slots}/
                            {queueSummary.total_slots}
                          </Badge>
                        </div>
                      </div>
                    </AccordionTrigger>
                    <AccordionContent>
                      <Table>
                        <TableHeader>
                          <TableRow>
                            <TableHead className="w-16">Slot</TableHead>
                            <TableHead>Worker ID</TableHead>
                            <TableHead className="text-right">
                              Expires In
                            </TableHead>
                          </TableRow>
                        </TableHeader>
                        <TableBody>
                          {queueSummary.slots.map((slot) => (
                            <TableRow
                              key={slot.slot}
                              className={slot.is_active ? "" : "opacity-50"}
                            >
                              <TableCell className="font-mono text-xs">
                                #{slot.slot}
                              </TableCell>
                              <TableCell className="font-mono text-xs">
                                {slot.locked_by || "—"}
                              </TableCell>
                              <TableCell className="text-right text-xs">
                                {slot.is_active ? (
                                  <Badge variant="outline" className="text-xs">
                                    <Clock className="mr-1 h-3 w-3" />
                                    {formatTimestamp(slot.locked_until)}
                                  </Badge>
                                ) : (
                                  <span className="text-muted-foreground">
                                    Available
                                  </span>
                                )}
                              </TableCell>
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                    </AccordionContent>
                  </AccordionItem>
                );
              })}
            </Accordion>
            {filteredProviders.length === 0 && (
              <p className="text-xs text-muted-foreground">
                No queue keys match the current filter.
              </p>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// =============================================================================
// Queue Health Summary Card
// =============================================================================

function QueueHealthCard() {
  const {
    data: slotsData,
    error: slotsError,
    isLoading: slotsLoading,
  } = useSWR<QueueSlotsResponse>("/api/admin/slots", fetcher, {
    refreshInterval: 10000,
  });

  const {
    data: qsData,
    error: qsError,
    isLoading: qsLoading,
  } = useSWR<QueueStatusResponse>("/api/admin/queue-status", fetcher, {
    refreshInterval: 10000,
  });
  const [queueFilter, setQueueFilter] = useState("");

  const queueKeys = new Set<string>();
  slotsData?.queue_keys.forEach((p) => queueKeys.add(p.queue_key));
  qsData?.trial_queues?.forEach((q) => queueKeys.add(q.queue_key));

  const queueRows = Array.from(queueKeys).map((queueKey) => {
    const slotSummary =
      slotsData?.queue_keys.find((p) => p.queue_key === queueKey) ?? null;
    const trialEntry = qsData?.trial_queues?.find(
      (q) => q.queue_key === queueKey,
    );
    const queued = trialEntry?.queued ?? 0;
    const running = trialEntry?.running ?? 0;
    const totalSlots = slotSummary?.total_slots ?? 0;
    const activeSlots = slotSummary?.active_slots ?? 0;
    const staleLocks =
      slotSummary?.slots.filter((slot) => slot.locked_by && !slot.is_active)
        .length ?? 0;

    const notes: string[] = [];
    if (totalSlots === 0 && (queued > 0 || running > 0)) {
      notes.push("No slots configured");
    }
    if (queued > 0 && totalSlots > 0 && activeSlots === 0) {
      notes.push("No active workers");
    }
    if (queued > 0 && totalSlots > 0 && activeSlots >= totalSlots) {
      notes.push("At capacity");
    }
    if (staleLocks > 0) {
      notes.push(`${staleLocks} stale lock${staleLocks > 1 ? "s" : ""}`);
    }

    return {
      queueKey,
      queued,
      running,
      totalSlots,
      activeSlots,
      notes,
    };
  });
  const filteredRows = queueRows
    .filter((row) =>
      row.queueKey.toLowerCase().includes(queueFilter.toLowerCase().trim()),
    )
    .sort((a, b) => b.queued + b.running - (a.queued + a.running))
    .slice(0, 30);

  const totalQueued = queueRows.reduce((sum, row) => sum + row.queued, 0);
  const totalRunning = queueRows.reduce((sum, row) => sum + row.running, 0);
  const totalSlots = slotsData?.total_slots ?? 0;
  const totalActive = slotsData?.total_active ?? 0;

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Server className="h-5 w-5" />
            <CardTitle className="text-base">Queue Health</CardTitle>
            {slotsData && (
              <Badge variant="outline" className="text-xs">
                {totalActive}/{totalSlots} slots active
              </Badge>
            )}
          </div>
          {qsData && (
            <div className="flex gap-2">
              <Badge variant="outline" className="text-xs">
                {totalQueued} queued
              </Badge>
              <Badge variant="outline" className="text-xs">
                {totalRunning} running
              </Badge>
            </div>
          )}
        </div>
        {qsData && (
          <p className="text-xs text-muted-foreground">
            Last updated: {new Date(qsData.timestamp).toLocaleTimeString()}
          </p>
        )}
      </CardHeader>
      <CardContent>
        {slotsError || qsError ? (
          <Alert variant="destructive">
            <AlertCircle className="h-4 w-4" />
            <AlertTitle>Failed to load queue health</AlertTitle>
            <AlertDescription>
              {slotsError instanceof Error
                ? slotsError.message
                : qsError instanceof Error
                  ? qsError.message
                  : "Check if you have admin access."}
            </AlertDescription>
          </Alert>
        ) : slotsLoading || qsLoading ? (
          <p className="text-muted-foreground">Loading...</p>
        ) : queueRows.length === 0 ? (
          <div className="py-6 text-center text-muted-foreground">
            <Server className="mx-auto mb-2 h-10 w-10 opacity-50" />
            <p>No queue data available</p>
          </div>
        ) : (
          <div className="space-y-3">
            <Input
              value={queueFilter}
              onChange={(event) => setQueueFilter(event.target.value)}
              placeholder="Filter queue keys..."
              className="h-8 text-xs"
            />
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Queue Key</TableHead>
                  <TableHead className="text-right">Queued</TableHead>
                  <TableHead className="text-right">Running</TableHead>
                  <TableHead className="text-right">Slots</TableHead>
                  <TableHead>Notes</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {filteredRows.map((row) => (
                  <TableRow key={row.queueKey}>
                    <TableCell>
                      <span className="inline-flex items-center gap-2">
                        <QueueKeyIcon queueKey={row.queueKey} size={13} />
                        <span className="font-mono text-xs">
                          {row.queueKey}
                        </span>
                      </span>
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs">
                      {row.queued}
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs">
                      {row.running}
                    </TableCell>
                    <TableCell className="text-right text-xs">
                      {row.activeSlots}/{row.totalSlots || "—"}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {row.notes.length > 0 ? row.notes.join(" • ") : "—"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <p className="text-xs text-muted-foreground">
              Running tracks active workers. If queued &gt; 0 with no active
              slots, workers are not spawning or slots are locked.
            </p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function formatIssueLabel(issue: string) {
  switch (issue) {
    case "running_stale_heartbeat":
      return "Running trial with stale heartbeat";
    case "active_task_without_active_trials":
      return "Active task without active trials";
    default:
      return issue.replaceAll("_", " ");
  }
}

function OrphanedStateCard() {
  const { data, error, isLoading } = useSWR<OrphanedStateResponse>(
    "/api/admin/orphaned-state?stale_after_minutes=10",
    fetcher,
    {
      refreshInterval: 10000,
    },
  );

  const counts = data?.counts;
  const totalIssues = counts
    ? counts.running_stale_heartbeat + counts.active_tasks_without_active_trials
    : 0;

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <AlertCircle className="h-5 w-5" />
            <CardTitle className="text-base">Orphaned State</CardTitle>
            {counts && (
              <Badge variant={totalIssues > 0 ? "destructive" : "outline"}>
                {totalIssues} signals
              </Badge>
            )}
          </div>
          {data && (
            <p className="text-xs text-muted-foreground">
              Updated {new Date(data.timestamp).toLocaleTimeString()}
            </p>
          )}
        </div>
        {data && (
          <p className="text-xs text-muted-foreground">
            Heartbeat becomes stale after {data.stale_after_minutes} minutes.
          </p>
        )}
      </CardHeader>
      <CardContent>
        {error ? (
          <Alert variant="destructive">
            <AlertCircle className="h-4 w-4" />
            <AlertTitle>Failed to load orphaned state</AlertTitle>
            <AlertDescription>
              {error instanceof Error ? error.message : "Unknown error"}
            </AlertDescription>
          </Alert>
        ) : isLoading || !counts ? (
          <p className="text-muted-foreground">Loading...</p>
        ) : (
          <div className="space-y-4">
            <div className="flex flex-wrap gap-2 text-xs">
              <Badge variant="outline">
                stale-heartbeat {counts.running_stale_heartbeat}
              </Badge>
              <Badge variant="outline">
                stuck-tasks {counts.active_tasks_without_active_trials}
              </Badge>
            </div>

            {totalIssues === 0 ? (
              <p className="text-sm text-muted-foreground">
                No orphaned queue or pipeline state detected.
              </p>
            ) : (
              <>
                <div className="space-y-2">
                  <div className="text-sm font-medium">
                    Execution job samples
                  </div>
                  {data.trial_samples.length === 0 ? (
                    <p className="text-xs text-muted-foreground">
                      No execution job samples.
                    </p>
                  ) : (
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Issue</TableHead>
                          <TableHead>Execution Job</TableHead>
                          <TableHead>Queue Key</TableHead>
                          <TableHead>Worker</TableHead>
                          <TableHead>Heartbeat</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {data.trial_samples.map((sample) => (
                          <TableRow key={`${sample.issue}-${sample.trial_id}`}>
                            <TableCell className="text-xs">
                              {formatIssueLabel(sample.issue)}
                            </TableCell>
                            <TableCell className="font-mono text-xs">
                              {sample.trial_id}
                            </TableCell>
                            <TableCell className="text-xs">
                              {sample.queue_key}
                            </TableCell>
                            <TableCell className="font-mono text-xs">
                              {sample.current_worker_id || "—"}
                            </TableCell>
                            <TableCell className="text-xs text-muted-foreground">
                              {formatAge(sample.heartbeat_at)}
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  )}
                </div>

                <div className="space-y-2">
                  <div className="text-sm font-medium">
                    Task-stage job samples
                  </div>
                  {data.task_samples.length === 0 ? (
                    <p className="text-xs text-muted-foreground">
                      No task samples.
                    </p>
                  ) : (
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Issue</TableHead>
                          <TableHead>Task</TableHead>
                          <TableHead>Status</TableHead>
                          <TableHead>Verdict</TableHead>
                          <TableHead>Updated</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {data.task_samples.map((sample) => (
                          <TableRow key={`${sample.issue}-${sample.task_id}`}>
                            <TableCell className="text-xs">
                              {formatIssueLabel(sample.issue)}
                            </TableCell>
                            <TableCell className="font-mono text-xs">
                              {sample.task_id}
                            </TableCell>
                            <TableCell className="text-xs">
                              {sample.status}
                            </TableCell>
                            <TableCell className="text-xs">
                              {sample.verdict_status || "—"}
                            </TableCell>
                            <TableCell className="text-xs text-muted-foreground">
                              {formatAge(sample.updated_at)}
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  )}
                </div>
              </>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// =============================================================================
// Queue Status Card
// =============================================================================

function QueueStatusCard() {
  const { data, error, isLoading, mutate } = useSWR<QueueStatusResponse>(
    "/api/admin/queue-status",
    fetcher,
    { refreshInterval: 5000 },
  );

  const totalQueued =
    (data?.trial_queues?.reduce((sum, q) => sum + q.queued, 0) ?? 0) +
    (data?.analysis_queued ?? 0) +
    (data?.verdict_queued ?? 0);
  const totalRunning =
    (data?.trial_queues?.reduce((sum, q) => sum + q.running, 0) ?? 0) +
    (data?.analysis_running ?? 0) +
    (data?.verdict_running ?? 0);

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Database className="h-5 w-5" />
            <CardTitle className="text-base">Queue Status</CardTitle>
            {data && (
              <div className="flex gap-2">
                <Badge variant="outline" className="text-xs">
                  {totalQueued} queued
                </Badge>
                <Badge variant="outline" className="text-xs">
                  {totalRunning} running
                </Badge>
              </div>
            )}
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={() => mutate()}
            disabled={isLoading}
          >
            <RefreshCw
              className={`h-4 w-4 ${isLoading ? "animate-spin" : ""}`}
            />
          </Button>
        </div>
        {data && (
          <p className="text-xs text-muted-foreground">
            Last updated: {new Date(data.timestamp).toLocaleTimeString()}
          </p>
        )}
      </CardHeader>
      <CardContent className="space-y-4">
        {error ? (
          <Alert variant="destructive">
            <AlertCircle className="h-4 w-4" />
            <AlertTitle>Failed to load queue status</AlertTitle>
            <AlertDescription>
              {error instanceof Error
                ? error.message
                : "Check if you have admin access."}
            </AlertDescription>
          </Alert>
        ) : isLoading && !data ? (
          <p className="text-muted-foreground">Loading...</p>
        ) : !data ||
          (data.trial_queues.length === 0 &&
            data.analysis_queued === 0 &&
            data.verdict_queued === 0) ? (
          <div className="py-8 text-center text-muted-foreground">
            <Database className="mx-auto mb-3 h-12 w-12 opacity-50" />
            <p>No active queue items</p>
          </div>
        ) : (
          <div className="space-y-4">
            {data.trial_queues.length > 0 && (
              <div className="space-y-3">
                <div className="text-sm font-medium">Trial Queues</div>
                {data.trial_queues.map((q) => {
                  const total = q.queued + q.running;
                  return (
                    <div key={q.queue_key} className="space-y-1">
                      <div className="flex items-center justify-between">
                        <span className="inline-flex items-center gap-1.5">
                          <QueueKeyIcon queueKey={q.queue_key} size={12} />
                          <span className="font-mono text-xs">
                            {q.queue_key}
                          </span>
                        </span>
                        <span className="text-xs text-muted-foreground">
                          {q.queued} queued · {q.running} running
                        </span>
                      </div>
                      <div className="flex h-2 gap-1">
                        {q.queued > 0 && (
                          <div
                            className="rounded-sm bg-purple-500"
                            style={{
                              width: `${(q.queued / total) * 100}%`,
                            }}
                            title={`Queued: ${q.queued}`}
                          />
                        )}
                        {q.running > 0 && (
                          <div
                            className="rounded-sm bg-blue-500"
                            style={{
                              width: `${(q.running / total) * 100}%`,
                            }}
                            title={`Running: ${q.running}`}
                          />
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}

            {(data.analysis_queued > 0 || data.analysis_running > 0) && (
              <div className="flex items-center justify-between text-xs">
                <span className="font-medium">Analysis</span>
                <span className="text-muted-foreground">
                  {data.analysis_queued} queued · {data.analysis_running}{" "}
                  running
                </span>
              </div>
            )}
            {(data.verdict_queued > 0 || data.verdict_running > 0) && (
              <div className="flex items-center justify-between text-xs">
                <span className="font-medium">Verdict</span>
                <span className="text-muted-foreground">
                  {data.verdict_queued} queued · {data.verdict_running} running
                </span>
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// =============================================================================
// Main Admin Page
// =============================================================================

export default function AdminPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Admin Dashboard</h1>
        <p className="text-sm text-muted-foreground">
          Internal system monitoring for workers and job queues
        </p>
      </div>

      <Tabs defaultValue="overview" className="space-y-4">
        <TabsList>
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="slots">Worker Slots</TabsTrigger>
          <TabsTrigger value="queue">Queue Status</TabsTrigger>
        </TabsList>

        <TabsContent value="overview" className="space-y-4">
          <QueuesAndPipelineCard />
          <QueueHealthCard />
          <OrphanedStateCard />
        </TabsContent>

        <TabsContent value="slots">
          <QueueSlotsCard />
        </TabsContent>

        <TabsContent value="queue">
          <QueueStatusCard />
        </TabsContent>
      </Tabs>
    </div>
  );
}

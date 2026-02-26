"use client";

import { useEffect, useMemo, useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Alert, AlertTitle, AlertDescription } from "@/components/ui/alert";
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
  PGQueuerResponse,
  QueueSlotSummary,
  PGQueuerJob,
} from "@/lib/types";
import { fetcher } from "@/lib/api";
import { QueueKeyIcon } from "@/components/queue-key-icon";
import {
  ChevronDown,
  RefreshCw,
  Server,
  Database,
  Clock,
  Zap,
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
        const queued = Number(stats.queued) || 0;
        const running = Number(stats.running) || 0;
        const retrying = Number(stats.retrying) || 0;
        const failed = Number(stats.failed) || 0;
        const recommended = Number(stats.recommended_concurrency) || 0;
        const backlog = queued + retrying;
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
        const mixFailed = getQueueDelta(windowSamples, queueKey, "failed");
        const deltaSuccess = getQueueDelta(windowSamples, queueKey, "success");
        const deltaFailed = getQueueDelta(windowSamples, queueKey, "failed");
        const trend = getQueueSeries(windowSamples, queueKey, (entry) => {
          const trendQueued = Number(entry?.queued) || 0;
          const trendRunning = Number(entry?.running) || 0;
          const trendRetrying = Number(entry?.retrying) || 0;
          return trendQueued + trendRunning + trendRetrying;
        });

        return {
          queueKey,
          queued,
          running,
          retrying,
          failed,
          recommended,
          backlog,
          mixQueued,
          mixRetrying,
          mixRunning,
          mixFailed,
          deltaSuccess,
          deltaFailed,
          trend,
        };
      })
      .sort((a, b) => b.backlog - a.backlog);
  }, [queues, queueKeys, windowSamples]);

  const totals = useMemo(() => {
    if (!queues) {
      return {
        backlog: 0,
        running: 0,
        failed: 0,
      };
    }
    return Object.values(queues).reduce(
      (acc, stats) => {
        const queued = Number(stats.queued) || 0;
        const running = Number(stats.running) || 0;
        const retrying = Number(stats.retrying) || 0;
        const failed = Number(stats.failed) || 0;
        return {
          backlog: acc.backlog + queued + retrying,
          running: acc.running + running,
          failed: acc.failed + failed,
        };
      },
      {
        backlog: 0,
        running: 0,
        failed: 0,
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
            Queued {totals.backlog}
          </Badge>
          <Badge variant="outline" className="text-[10px] font-normal">
            Running {totals.running}
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
          <div className="text-center py-6 text-muted-foreground text-sm">
            No active queue keys yet.
          </div>
        ) : rows.length === 0 ? (
          <div className="text-center py-6 text-muted-foreground text-sm">
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
                  {
                    key: "failed",
                    label: "Failed",
                    value: row.mixFailed,
                    className: "bg-red-400/80",
                    textClassName: "text-red-400",
                  },
                ];

                return (
                  <TableRow key={row.queueKey}>
                    <TableCell className="font-medium">
                      <span className="inline-flex items-center gap-2">
                        <QueueKeyIcon queueKey={row.queueKey} size={13} />
                        <span className="capitalize">{row.queueKey}</span>
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
          <div className="text-center py-8 text-muted-foreground">
            <Server className="h-12 w-12 mx-auto mb-3 opacity-50" />
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
                    className="border rounded-lg px-3"
                  >
                    <AccordionTrigger className="hover:no-underline py-3">
                      <div className="flex items-center justify-between w-full pr-2">
                        <span className="font-medium">
                          <span className="inline-flex items-center gap-2">
                            <QueueKeyIcon queueKey={queueKey} size={13} />
                            <span className="capitalize">{queueKey}</span>
                          </span>
                        </span>
                        <div className="flex items-center gap-2">
                          <div className="flex gap-1">
                            {Array.from({
                              length: queueSummary.total_slots,
                            }).map((_, i) => (
                              <div
                                key={i}
                                className={`w-2 h-2 rounded-full ${
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
                                    <Clock className="h-3 w-3 mr-1" />
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
    data: pgData,
    error: pgError,
    isLoading: pgLoading,
  } = useSWR<PGQueuerResponse>("/api/admin/pgqueuer?page_size=1", fetcher, {
    refreshInterval: 10000,
  });
  const [queueFilter, setQueueFilter] = useState("");

  const queueKeys = new Set<string>();
  slotsData?.queue_keys.forEach((p) => queueKeys.add(p.queue_key));
  Object.keys(pgData?.stats?.by_entrypoint ?? {}).forEach((p) =>
    queueKeys.add(p),
  );

  const queueRows = Array.from(queueKeys).map((queueKey) => {
    const slotSummary =
      slotsData?.queue_keys.find((p) => p.queue_key === queueKey) ?? null;
    const statusCounts = pgData?.stats?.by_entrypoint?.[queueKey] ?? {};
    const queued = statusCounts.queued ?? 0;
    const picked = statusCounts.picked ?? 0;
    const totalSlots = slotSummary?.total_slots ?? 0;
    const activeSlots = slotSummary?.active_slots ?? 0;
    const staleLocks =
      slotSummary?.slots.filter((slot) => slot.locked_by && !slot.is_active)
        .length ?? 0;

    const notes: string[] = [];
    if (totalSlots === 0 && (queued > 0 || picked > 0)) {
      notes.push("No slots configured");
    }
    if (queued > 0 && totalSlots > 0 && activeSlots === 0) {
      notes.push("No active workers");
    }
    if (queued > 0 && totalSlots > 0 && activeSlots >= totalSlots) {
      notes.push("At capacity");
    }
    if (picked > activeSlots && activeSlots > 0) {
      notes.push("Picked exceeds slots");
    }
    if (staleLocks > 0) {
      notes.push(`${staleLocks} stale lock${staleLocks > 1 ? "s" : ""}`);
    }

    return {
      queueKey,
      queued,
      picked,
      totalSlots,
      activeSlots,
      notes,
    };
  });
  const filteredRows = queueRows
    .filter((row) =>
      row.queueKey.toLowerCase().includes(queueFilter.toLowerCase().trim()),
    )
    .sort((a, b) => b.queued + b.picked - (a.queued + a.picked))
    .slice(0, 30);

  const totalQueued = queueRows.reduce((sum, row) => sum + row.queued, 0);
  const totalPicked = queueRows.reduce((sum, row) => sum + row.picked, 0);
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
          {pgData && (
            <div className="flex gap-2">
              <Badge variant="outline" className="text-xs">
                {totalQueued} queued
              </Badge>
              <Badge variant="outline" className="text-xs">
                {totalPicked} picked
              </Badge>
            </div>
          )}
        </div>
        {pgData && (
          <p className="text-xs text-muted-foreground">
            Last updated: {new Date(pgData.timestamp).toLocaleTimeString()}
          </p>
        )}
      </CardHeader>
      <CardContent>
        {slotsError || pgError ? (
          <Alert variant="destructive">
            <AlertCircle className="h-4 w-4" />
            <AlertTitle>Failed to load queue health</AlertTitle>
            <AlertDescription>
              {slotsError instanceof Error
                ? slotsError.message
                : pgError instanceof Error
                  ? pgError.message
                  : "Check if you have admin access."}
            </AlertDescription>
          </Alert>
        ) : slotsLoading || pgLoading ? (
          <p className="text-muted-foreground">Loading...</p>
        ) : queueRows.length === 0 ? (
          <div className="text-center py-6 text-muted-foreground">
            <Server className="h-10 w-10 mx-auto mb-2 opacity-50" />
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
                  <TableHead className="text-right">Picked</TableHead>
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
                        <span className="capitalize">{row.queueKey}</span>
                      </span>
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs">
                      {row.queued}
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs">
                      {row.picked}
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
              Picked tracks running workers. If queued &gt; 0 with no active
              slots, workers are not spawning or slots are locked.
            </p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// =============================================================================
// PGQueuer Jobs Card
// =============================================================================

function PGQueuerCard() {
  const [page, setPage] = useState(1);
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [entrypointFilter, setEntrypointFilter] = useState<string>("all");
  const [entrypointQuery, setEntrypointQuery] = useState("");

  const queryParams = new URLSearchParams();
  queryParams.set("page", String(page));
  queryParams.set("page_size", "50");
  if (statusFilter !== "all") queryParams.set("status", statusFilter);
  if (entrypointFilter !== "all")
    queryParams.set("entrypoint", entrypointFilter);

  const { data, error, isLoading, mutate } = useSWR<PGQueuerResponse>(
    `/api/admin/pgqueuer?${queryParams.toString()}`,
    fetcher,
    { refreshInterval: 5000 },
  );

  const formatDate = (dateStr: string | null) => {
    if (!dateStr) return "—";
    return new Date(dateStr).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  };

  const getOldestAge = (status: string) => {
    if (!data?.jobs) return "—";
    const candidates = data.jobs
      .filter((job) => job.status === status && job.created)
      .map((job) => job.created as string);
    if (candidates.length === 0) return "—";
    const oldest = candidates.reduce((min, current) =>
      new Date(current).getTime() < new Date(min).getTime() ? current : min,
    );
    return formatAge(oldest);
  };

  const getStatusColor = (status: string) => {
    switch (status) {
      case "queued":
        return "bg-purple-500/10 text-purple-400 border-purple-500/30";
      case "picked":
        return "bg-blue-500/10 text-blue-400 border-blue-500/30";
      case "success":
        return "bg-green-500/10 text-green-400 border-green-500/30";
      case "failed":
        return "bg-red-500/10 text-red-400 border-red-500/30";
      case "cancelled":
        return "bg-gray-500/10 text-gray-400 border-gray-500/30";
      default:
        return "bg-yellow-500/10 text-yellow-400 border-yellow-500/30";
    }
  };

  const entrypoints = data?.stats?.by_entrypoint
    ? Object.keys(data.stats.by_entrypoint).filter((key) =>
        key.toLowerCase().includes(entrypointQuery.toLowerCase().trim()),
      )
    : [];
  const statuses = data?.stats?.by_status
    ? Object.keys(data.stats.by_status)
    : [];

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Database className="h-5 w-5" />
            <CardTitle className="text-base">PGQueuer Jobs</CardTitle>
            {data && (
              <Badge variant="outline" className="text-xs">
                {data.stats.total} total
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
      <CardContent className="space-y-4">
        {/* Stats Overview */}
        {data && (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
            {Object.entries(data.stats.by_status).map(([status, count]) => (
              <div
                key={status}
                className="p-2 rounded-md border border-border text-center"
              >
                <div className="text-lg font-bold">{count}</div>
                <div className="text-xs text-muted-foreground capitalize">
                  {status}
                </div>
              </div>
            ))}
          </div>
        )}
        {data && (
          <div className="flex flex-wrap gap-3 text-xs text-muted-foreground">
            <span>Oldest queued (page): {getOldestAge("queued")}</span>
            <span>Oldest picked (page): {getOldestAge("picked")}</span>
          </div>
        )}

        {/* Filters */}
        <div className="flex flex-wrap gap-2">
          <Select value={statusFilter} onValueChange={setStatusFilter}>
            <SelectTrigger className="w-[140px]">
              <SelectValue placeholder="Status" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All statuses</SelectItem>
              {statuses.map((s) => (
                <SelectItem key={s} value={s}>
                  {s}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Select value={entrypointFilter} onValueChange={setEntrypointFilter}>
            <SelectTrigger className="w-[220px]">
              <SelectValue placeholder="Queue key" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All queue keys</SelectItem>
              {entrypoints.map((ep) => (
                <SelectItem key={ep} value={ep}>
                  {ep}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Input
            value={entrypointQuery}
            onChange={(event) => setEntrypointQuery(event.target.value)}
            placeholder="Search queue keys..."
            className="h-9 w-[220px] text-xs"
          />
        </div>

        {/* Jobs Table */}
        {error ? (
          <Alert variant="destructive">
            <AlertCircle className="h-4 w-4" />
            <AlertTitle>Failed to load jobs</AlertTitle>
            <AlertDescription>
              {error instanceof Error
                ? error.message
                : "Check if you have admin access."}
            </AlertDescription>
          </Alert>
        ) : isLoading && !data ? (
          <p className="text-muted-foreground">Loading...</p>
        ) : !data || data.jobs.length === 0 ? (
          <div className="text-center py-8 text-muted-foreground">
            <Database className="h-12 w-12 mx-auto mb-3 opacity-50" />
            <p>No jobs in queue</p>
          </div>
        ) : (
          <>
            <div className="max-h-[400px] overflow-y-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-16">ID</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>Queue Key</TableHead>
                    <TableHead>Job Type</TableHead>
                    <TableHead>Target ID</TableHead>
                    <TableHead>Age</TableHead>
                    <TableHead className="text-right">Created</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.jobs.map((job: PGQueuerJob) => {
                    const jobType = job.payload?.job_type as string | undefined;
                    const trialId = job.payload?.trial_id as string | undefined;
                    const taskId = job.payload?.task_id as string | undefined;
                    const targetId = trialId || taskId || "—";

                    return (
                      <TableRow key={job.id}>
                        <TableCell className="font-mono text-xs">
                          {job.id}
                        </TableCell>
                        <TableCell>
                          <Badge
                            variant="outline"
                            className={`text-xs ${getStatusColor(job.status)}`}
                          >
                            {job.status}
                          </Badge>
                        </TableCell>
                        <TableCell className="text-xs">
                          <span className="inline-flex items-center gap-1.5">
                            <QueueKeyIcon queueKey={job.entrypoint} size={12} />
                            <span className="capitalize">{job.entrypoint}</span>
                          </span>
                        </TableCell>
                        <TableCell className="text-xs">
                          {jobType ? (
                            <Badge variant="secondary" className="text-xs">
                              <Zap className="h-3 w-3 mr-1" />
                              {jobType}
                            </Badge>
                          ) : (
                            "—"
                          )}
                        </TableCell>
                        <TableCell className="font-mono text-xs max-w-[120px] truncate">
                          {targetId}
                        </TableCell>
                        <TableCell className="text-xs text-muted-foreground">
                          {formatAge(job.created)}
                        </TableCell>
                        <TableCell className="text-right text-xs text-muted-foreground">
                          {formatDate(job.created)}
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>

            {/* Pagination */}
            <div className="flex items-center justify-between">
              <div className="text-xs text-muted-foreground">
                Page {data.page}
              </div>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  disabled={page === 1}
                >
                  Previous
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setPage((p) => p + 1)}
                  disabled={!data.has_more}
                >
                  Next
                </Button>
              </div>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}

// =============================================================================
// Entrypoint Stats Card
// =============================================================================

function EntrypointStatsCard() {
  const { data } = useSWR<PGQueuerResponse>(
    "/api/admin/pgqueuer?page_size=1",
    fetcher,
    { refreshInterval: 10000 },
  );

  if (!data?.stats?.by_entrypoint) return null;

  const entrypoints = Object.entries(data.stats.by_entrypoint);
  if (entrypoints.length === 0) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Queue by Key</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="space-y-3">
          {entrypoints.map(([entrypoint, statuses]) => {
            const total = Object.values(statuses).reduce((a, b) => a + b, 0);
            const queued = statuses.queued || 0;
            const picked = statuses.picked || 0;

            return (
              <div key={entrypoint} className="space-y-1">
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium capitalize">
                    {entrypoint}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    {total} total
                  </span>
                </div>
                <div className="flex gap-1 h-2">
                  {queued > 0 && (
                    <div
                      className="bg-purple-500 rounded-sm"
                      style={{ width: `${(queued / total) * 100}%` }}
                      title={`Queued: ${queued}`}
                    />
                  )}
                  {picked > 0 && (
                    <div
                      className="bg-blue-500 rounded-sm"
                      style={{ width: `${(picked / total) * 100}%` }}
                      title={`Picked: ${picked}`}
                    />
                  )}
                </div>
                <div className="flex gap-3 text-xs text-muted-foreground">
                  {Object.entries(statuses).map(([status, count]) => (
                    <span key={status}>
                      {status}: {count}
                    </span>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
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
        <p className="text-muted-foreground text-sm">
          Internal system monitoring for workers and job queues
        </p>
      </div>

      <Tabs defaultValue="overview" className="space-y-4">
        <TabsList>
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="slots">Worker Slots</TabsTrigger>
          <TabsTrigger value="pgqueuer">Job Queue</TabsTrigger>
        </TabsList>

        <TabsContent value="overview" className="space-y-4">
          <QueuesAndPipelineCard />
          <QueueHealthCard />
          <EntrypointStatsCard />
          <PGQueuerCard />
        </TabsContent>

        <TabsContent value="slots">
          <QueueSlotsCard />
        </TabsContent>

        <TabsContent value="pgqueuer">
          <PGQueuerCard />
        </TabsContent>
      </Tabs>
    </div>
  );
}

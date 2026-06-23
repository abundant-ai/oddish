"use client";

import { useEffect, useMemo, useState } from "react";
import useSWR from "swr";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Alert, AlertTitle, AlertDescription } from "@/components/ui/alert";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type {
  DashboardResponse,
  JobUsage,
  ModelUsage,
  QueueStats,
} from "@/lib/types";
import { fetcher } from "@/lib/api";
import { buildDashboardApiPath } from "@/lib/dashboard-request";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { Beaker, ChevronDown, Gavel, Loader2, Microscope } from "lucide-react";
import { QueueKeyIcon } from "@/components/queue-key-icon";

export function useDashboardUsage(
  usageMinutes: number | null,
  fallbackData?: DashboardResponse | null
) {
  const swrKey = buildDashboardApiPath({
    include_tasks: false,
    include_experiments: false,
    usage_minutes: usageMinutes,
  });
  const hasFallbackData = fallbackData != null;

  const { data, error, isLoading, isValidating } = useSWR<DashboardResponse>(
    swrKey,
    fetcher,
    {
      refreshInterval: (latestData) => {
        if (!latestData) return 5000;
        const hasActiveQueue = Object.values(latestData.queues ?? {}).some(
          (stats) =>
            (Number(stats.running) || 0) > 0 ||
            (Number(stats.queued) || 0) > 0 ||
            (Number(stats.retrying) || 0) > 0
        );
        return hasActiveQueue ? 30000 : 90000;
      },
      revalidateOnFocus: false,
      revalidateOnMount: !hasFallbackData,
      revalidateIfStale: !hasFallbackData,
      keepPreviousData: true,
      fallbackData: fallbackData ?? undefined,
    }
  );

  return {
    queues: data?.queues ?? null,
    pipeline: data?.pipeline ?? null,
    modelUsage: data?.model_usage ?? [],
    jobUsage: data?.job_usage ?? [],
    swrKey,
    cached: data?.cached ?? false,
    error,
    isLoading,
    isRefreshing: !error && !isLoading && isValidating,
  };
}

// =============================================================================
// Usage Overview
// =============================================================================

const TIME_RANGES = [
  { key: "all", label: "All", minutes: null },
  { key: "15m", label: "15m", minutes: 15 },
  { key: "1h", label: "1h", minutes: 60 },
  { key: "24h", label: "24h", minutes: 1440 },
  { key: "7d", label: "7d", minutes: 10080 },
  { key: "30d", label: "30d", minutes: 43200 },
  { key: "60d", label: "60d", minutes: 86400 },
] as const;

type PresetTimeRangeKey = (typeof TIME_RANGES)[number]["key"];
export type TimeRangeKey = PresetTimeRangeKey | `custom:${number}`;

export function getMinutesFromTimeRange(range: TimeRangeKey): number | null {
  if (range.startsWith("custom:")) {
    const value = Number(range.slice("custom:".length));
    return Number.isFinite(value) && value > 0 ? Math.round(value) : null;
  }
  return TIME_RANGES.find((r) => r.key === range)?.minutes ?? null;
}

function getTimeRangeLabel(range: TimeRangeKey): string {
  if (!range.startsWith("custom:")) {
    return TIME_RANGES.find((entry) => entry.key === range)?.label ?? "Window";
  }

  const minutes = getMinutesFromTimeRange(range);
  if (!minutes) return "Custom";
  if (minutes % 1440 === 0) return `${minutes / 1440}d`;
  if (minutes % 60 === 0) return `${minutes / 60}h`;
  return `${minutes}m`;
}

function formatCompactNumber(n: number): string {
  if (n >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(1)}B`;
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

function formatCost(usd: number): string {
  if (usd >= 100) return `$${usd.toFixed(0)}`;
  if (usd >= 1) return `$${usd.toFixed(2)}`;
  if (usd >= 0.01) return `$${usd.toFixed(3)}`;
  if (usd > 0) return `$${usd.toFixed(4)}`;
  return "$0";
}

function formatDuration(seconds: number | null): string {
  if (seconds === null) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

function inferProviderFromQueueKey(queueKey: string): string {
  const [provider] = queueKey.split("/", 1);
  return provider || "unknown";
}

function getQueueQueuedJobs(stats?: QueueStats[string]): number {
  if (!stats) return 0;
  return (Number(stats.pending) || 0) + (Number(stats.queued) || 0);
}

function getQueueTotalJobs(stats?: QueueStats[string]): number {
  if (!stats) return 0;
  return (
    (Number(stats.pending) || 0) +
    (Number(stats.queued) || 0) +
    (Number(stats.running) || 0) +
    (Number(stats.retrying) || 0) +
    (Number(stats.success) || 0) +
    (Number(stats.failed) || 0)
  );
}

type UsageRow = {
  key: string;
  queueKey: string;
  model: string;
  provider: string;
  jobCount: number;
  inputTokens: number;
  outputTokens: number;
  cacheTokens: number;
  costUsd: number;
  running: number;
  queued: number;
  retrying: number;
  avgDurationS: number | null;
  hasUsageMetrics: boolean;
};

const PIPELINE_KIND_DISPLAY: Record<
  string,
  {
    label: string;
    description: string;
    Icon: typeof Beaker;
    accentText: string;
    accentBorder: string;
  }
> = {
  TRIAL: {
    label: "Trials",
    description: "Harbor agent runs",
    Icon: Beaker,
    accentText: "text-blue-500 dark:text-blue-300",
    accentBorder: "border-blue-500/30",
  },
  QA: {
    label: "Task QA",
    description: "Classify trials + synthesize verdict",
    Icon: Gavel,
    accentText: "text-amber-500 dark:text-amber-300",
    accentBorder: "border-amber-500/30",
  },
  VERDICT: {
    label: "Task Verdict (legacy)",
    description: "Folded into Task QA",
    Icon: Gavel,
    accentText: "text-amber-500/70 dark:text-amber-300/70",
    accentBorder: "border-amber-500/30",
  },
  ANALYSIS: {
    label: "Trial Analysis (legacy)",
    description: "Folded into Task QA",
    Icon: Microscope,
    accentText: "text-purple-500 dark:text-purple-300",
    accentBorder: "border-purple-500/30",
  },
};

function getPipelineKindDisplay(kind: string) {
  return (
    PIPELINE_KIND_DISPLAY[kind] ?? {
      label: kind
        .toLowerCase()
        .split("_")
        .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
        .join(" "),
      description: "Worker job",
      Icon: Beaker,
      accentText: "text-slate-500 dark:text-slate-300",
      accentBorder: "border-slate-500/30",
    }
  );
}

// Compact tooltip surface used on the Status header badges to drill
// down into "N running" → TRIAL / ANALYSIS / VERDICT splits. The
// aggregate numbers would otherwise collapse all three kinds into a
// single counter -- hovering exposes which agent-job kind is actually
// backed up.
function KindBreakdownTooltip({
  pipeline,
  metric,
}: {
  pipeline: Record<
    string,
    { running: number; queued: number; retrying: number }
  >;
  metric: "running" | "queued" | "retrying";
}) {
  const kinds = Object.keys(pipeline).sort();
  const total = kinds.reduce((sum, k) => sum + pipeline[k][metric], 0);

  return (
    <div className="space-y-1 py-1 text-left text-[11px]">
      <div className="font-medium capitalize">{metric}</div>
      {kinds.map((kind) => {
        const display = getPipelineKindDisplay(kind);
        const Icon = display.Icon;
        const value = pipeline[kind][metric];
        return (
          <div key={kind} className="flex items-center justify-between gap-3">
            <span
              className={`inline-flex items-center gap-1 ${
                value > 0 ? display.accentText : "text-muted-foreground"
              }`}
            >
              <Icon className="h-3 w-3" />
              {display.label}
            </span>
            <span
              className={`font-mono tabular-nums ${
                value > 0 ? "" : "text-muted-foreground/60"
              }`}
            >
              {value}
            </span>
          </div>
        );
      })}
      <div className="border-border/40 text-muted-foreground border-t pt-1">
        Total: <span className="font-mono tabular-nums">{total}</span>
      </div>
    </div>
  );
}

export function UsageOverviewCard({
  queues,
  modelUsage,
  jobUsage,
  error,
  isLoading,
  isRefreshing,
  timeRange,
  onTimeRangeChange,
}: {
  queues: QueueStats | null;
  modelUsage: ModelUsage[];
  jobUsage: JobUsage[];
  error: Error | undefined;
  isLoading: boolean;
  isRefreshing: boolean;
  timeRange: TimeRangeKey;
  onTimeRangeChange: (key: TimeRangeKey) => void;
}) {
  const [isCustomPickerOpen, setIsCustomPickerOpen] = useState(
    timeRange.startsWith("custom:")
  );
  const [customMagnitude, setCustomMagnitude] = useState("2");
  const [customUnit, setCustomUnit] = useState<"m" | "h" | "d">("h");

  useEffect(() => {
    if (!timeRange.startsWith("custom:")) return;
    const minutes = getMinutesFromTimeRange(timeRange);
    if (!minutes) return;
    if (minutes % 1440 === 0) {
      setCustomMagnitude(String(minutes / 1440));
      setCustomUnit("d");
      return;
    }
    if (minutes % 60 === 0) {
      setCustomMagnitude(String(minutes / 60));
      setCustomUnit("h");
      return;
    }
    setCustomMagnitude(String(minutes));
    setCustomUnit("m");
  }, [timeRange]);

  const usageRows = useMemo(() => {
    const mergedRows = new Map<string, UsageRow>();
    const jobUsageByQueue = new Map<
      string,
      {
        jobCount: number;
        running: number;
        queued: number;
        retrying: number;
        durationTotalS: number;
        durationCount: number;
        avgDurationS: number | null;
      }
    >();

    for (const job of jobUsage) {
      const existing = jobUsageByQueue.get(job.queue_key) ?? {
        jobCount: 0,
        running: 0,
        queued: 0,
        retrying: 0,
        durationTotalS: 0,
        durationCount: 0,
        avgDurationS: null,
      };
      existing.jobCount += job.job_count;
      existing.running += job.running;
      existing.queued += job.queued;
      existing.retrying += job.retrying;
      if (job.avg_duration_s != null && job.job_count > 0) {
        existing.durationTotalS += job.avg_duration_s * job.job_count;
        existing.durationCount += job.job_count;
        existing.avgDurationS =
          existing.durationCount > 0
            ? existing.durationTotalS / existing.durationCount
            : null;
      }
      jobUsageByQueue.set(job.queue_key, existing);
    }

    for (const usage of modelUsage) {
      const queueKey = usage.model || usage.provider || "unknown";
      const jobsForQueue = jobUsageByQueue.get(queueKey);
      const queueStats = queues?.[queueKey];
      mergedRows.set(queueKey, {
        key: queueKey,
        queueKey,
        model: usage.model,
        provider: usage.provider,
        jobCount:
          jobsForQueue?.jobCount ||
          getQueueTotalJobs(queueStats) ||
          usage.trial_count,
        inputTokens: usage.input_tokens,
        outputTokens: usage.output_tokens,
        cacheTokens: usage.cache_tokens,
        costUsd: usage.cost_usd,
        running:
          jobsForQueue?.running ??
          (queueStats ? Number(queueStats.running) || 0 : usage.running),
        queued:
          jobsForQueue?.queued ??
          (queueStats ? getQueueQueuedJobs(queueStats) : usage.queued),
        retrying:
          jobsForQueue?.retrying ??
          (queueStats ? Number(queueStats.retrying) || 0 : 0),
        avgDurationS: jobsForQueue?.avgDurationS ?? usage.avg_duration_s,
        hasUsageMetrics: true,
      });
    }

    for (const [queueKey, jobsForQueue] of jobUsageByQueue) {
      if (mergedRows.has(queueKey)) continue;

      mergedRows.set(queueKey, {
        key: queueKey,
        queueKey,
        model: queueKey,
        provider: inferProviderFromQueueKey(queueKey),
        jobCount: jobsForQueue.jobCount,
        inputTokens: 0,
        outputTokens: 0,
        cacheTokens: 0,
        costUsd: 0,
        running: jobsForQueue.running,
        queued: jobsForQueue.queued,
        retrying: jobsForQueue.retrying,
        avgDurationS: jobsForQueue.avgDurationS,
        hasUsageMetrics: false,
      });
    }

    for (const [queueKey, queueStats] of Object.entries(queues ?? {})) {
      const totalJobs = getQueueTotalJobs(queueStats);
      if (mergedRows.has(queueKey) || totalJobs === 0) continue;

      mergedRows.set(queueKey, {
        key: queueKey,
        queueKey,
        model: queueKey,
        provider: inferProviderFromQueueKey(queueKey),
        jobCount: totalJobs,
        inputTokens: 0,
        outputTokens: 0,
        cacheTokens: 0,
        costUsd: 0,
        running: Number(queueStats.running) || 0,
        queued: getQueueQueuedJobs(queueStats),
        retrying: Number(queueStats.retrying) || 0,
        avgDurationS: null,
        hasUsageMetrics: false,
      });
    }

    return Array.from(mergedRows.values());
  }, [jobUsage, modelUsage, queues]);

  const sortedUsageRows = useMemo(
    () =>
      [...usageRows].sort((a, b) => {
        const aActive = a.running + a.queued + a.retrying;
        const bActive = b.running + b.queued + b.retrying;
        if (aActive !== bActive) return bActive - aActive;
        if (a.hasUsageMetrics !== b.hasUsageMetrics) {
          return a.hasUsageMetrics ? -1 : 1;
        }
        if (a.costUsd !== b.costUsd) return b.costUsd - a.costUsd;
        if (a.jobCount !== b.jobCount) return b.jobCount - a.jobCount;
        return a.model.localeCompare(b.model);
      }),
    [usageRows]
  );

  const totals = useMemo(
    () =>
      usageRows.reduce(
        (acc, row) => ({
          jobs: acc.jobs + row.jobCount,
          inputTokens: acc.inputTokens + row.inputTokens,
          outputTokens: acc.outputTokens + row.outputTokens,
          cacheTokens: acc.cacheTokens + row.cacheTokens,
          cost: acc.cost + row.costUsd,
          running: acc.running + row.running,
          queued: acc.queued + row.queued,
          retrying: acc.retrying + row.retrying,
        }),
        {
          jobs: 0,
          inputTokens: 0,
          outputTokens: 0,
          cacheTokens: 0,
          cost: 0,
          running: 0,
          queued: 0,
          retrying: 0,
        }
      ),
    [usageRows]
  );

  // Pipeline aggregation: group active counts by actual worker_jobs kind.
  const pipelineByKind = useMemo(() => {
    const kinds: Record<
      string,
      { running: number; queued: number; retrying: number }
    > = {};
    for (const job of jobUsage) {
      const kind = job.kind;
      kinds[kind] ??= { running: 0, queued: 0, retrying: 0 };
      kinds[kind].running += job.running;
      kinds[kind].queued += job.queued;
      kinds[kind].retrying += job.retrying;
    }
    return kinds;
  }, [jobUsage]);

  const selectedWindowValue = timeRange.startsWith("custom:")
    ? "custom"
    : timeRange;
  const selectedWindowLabel = getTimeRangeLabel(timeRange);
  const showCustomControls =
    isCustomPickerOpen || timeRange.startsWith("custom:");

  const applyCustomWindow = () => {
    const magnitude = Number(customMagnitude);
    if (!Number.isFinite(magnitude) || magnitude <= 0) return;
    const roundedMagnitude = Math.round(magnitude);
    const minutesPerUnit =
      customUnit === "d" ? 1440 : customUnit === "h" ? 60 : 1;
    const minutes = Math.min(
      86400,
      Math.max(1, roundedMagnitude * minutesPerUnit)
    );
    onTimeRangeChange(`custom:${minutes}`);
    setIsCustomPickerOpen(false);
  };

  return (
    <Card className="border-[#6f88b4]/20 shadow-xs">
      <CardHeader className="pb-2">
        <div className="flex flex-wrap items-start gap-2 sm:items-center">
          <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
            <CardTitle className="text-base">Usage</CardTitle>
            {(isLoading || isRefreshing) && (
              <Badge
                variant="outline"
                className="text-muted-foreground text-[10px] font-normal"
              >
                <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                {isLoading ? "Loading" : "Updating"}
              </Badge>
            )}
            <TooltipProvider delayDuration={150}>
              {totals.running > 0 && (
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Badge
                      variant="outline"
                      className="cursor-help border-[#85b85c]/30 text-[10px] font-normal text-[#5c8e43] dark:text-[#85b85c]"
                    >
                      {totals.running} running
                    </Badge>
                  </TooltipTrigger>
                  <TooltipContent className="w-[220px]">
                    <KindBreakdownTooltip
                      pipeline={pipelineByKind}
                      metric="running"
                    />
                  </TooltipContent>
                </Tooltip>
              )}
              {totals.queued > 0 && (
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Badge
                      variant="outline"
                      className="cursor-help border-[#6f88b4]/30 text-[10px] font-normal text-[#5d77a5] dark:text-[#a8b8d2]"
                    >
                      {totals.queued} queued
                    </Badge>
                  </TooltipTrigger>
                  <TooltipContent className="w-[220px]">
                    <KindBreakdownTooltip
                      pipeline={pipelineByKind}
                      metric="queued"
                    />
                  </TooltipContent>
                </Tooltip>
              )}
              {totals.retrying > 0 && (
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Badge
                      variant="outline"
                      className="cursor-help border-amber-500/30 text-[10px] font-normal text-amber-600 dark:text-amber-300"
                    >
                      {totals.retrying} retrying
                    </Badge>
                  </TooltipTrigger>
                  <TooltipContent className="w-[220px]">
                    <KindBreakdownTooltip
                      pipeline={pipelineByKind}
                      metric="retrying"
                    />
                  </TooltipContent>
                </Tooltip>
              )}
            </TooltipProvider>
          </div>
          <div className="ml-auto flex shrink-0 flex-wrap items-center justify-end gap-1">
            <DropdownMenu modal={false}>
              <DropdownMenuTrigger asChild>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="h-7 w-[120px] justify-between border-[#6f88b4]/20 px-2 text-[11px]"
                  aria-label="Time window"
                >
                  <span>{selectedWindowLabel}</span>
                  <ChevronDown className="h-3.5 w-3.5 opacity-50" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-[120px]">
                <DropdownMenuRadioGroup
                  value={selectedWindowValue}
                  onValueChange={(value) => {
                    if (value === "custom") {
                      setIsCustomPickerOpen(true);
                      return;
                    }
                    setIsCustomPickerOpen(false);
                    onTimeRangeChange(value as PresetTimeRangeKey);
                  }}
                >
                  {TIME_RANGES.map((range) => (
                    <DropdownMenuRadioItem
                      key={range.key}
                      value={range.key}
                      className="text-xs"
                    >
                      {range.label}
                    </DropdownMenuRadioItem>
                  ))}
                  <DropdownMenuRadioItem value="custom" className="text-xs">
                    Custom...
                  </DropdownMenuRadioItem>
                </DropdownMenuRadioGroup>
              </DropdownMenuContent>
            </DropdownMenu>
            {showCustomControls && (
              <>
                <Input
                  value={customMagnitude}
                  onChange={(event) => setCustomMagnitude(event.target.value)}
                  inputMode="numeric"
                  className="h-7 w-[66px] text-[11px]"
                  aria-label="Custom time window amount"
                />
                <Select
                  value={customUnit}
                  onValueChange={(value) =>
                    setCustomUnit(value as "m" | "h" | "d")
                  }
                >
                  <SelectTrigger
                    className="h-7 w-[72px] border-[#6f88b4]/20 text-[11px]"
                    aria-label="Custom time window unit"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent align="end">
                    <SelectItem value="m" className="text-xs">
                      min
                    </SelectItem>
                    <SelectItem value="h" className="text-xs">
                      hour
                    </SelectItem>
                    <SelectItem value="d" className="text-xs">
                      day
                    </SelectItem>
                  </SelectContent>
                </Select>
                <Button
                  variant="secondary"
                  size="sm"
                  className="h-7 px-2 text-[11px]"
                  onClick={applyCustomWindow}
                >
                  Apply
                </Button>
              </>
            )}
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {error ? (
          <Alert variant="destructive">
            <AlertTitle>Dashboard unavailable</AlertTitle>
            <AlertDescription>Failed to load usage data.</AlertDescription>
          </Alert>
        ) : isLoading && modelUsage.length === 0 && jobUsage.length === 0 ? (
          <div className="text-muted-foreground flex items-center gap-2 py-6 text-sm">
            <Loader2 className="h-4 w-4 animate-spin" />
            Loading usage data...
          </div>
        ) : (
          <>
            {isRefreshing && (
              <div className="text-muted-foreground flex items-center gap-2 text-xs">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                Refreshing usage data...
              </div>
            )}
            {/* Summary stats row */}
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <div className="bg-background/70 rounded-md border border-[#6f88b4]/18 p-2 text-center">
                <div className="text-base font-bold tabular-nums">
                  {formatCost(totals.cost)}
                </div>
                <div className="text-muted-foreground text-[10px]">Cost</div>
              </div>
              <div className="bg-background/70 rounded-md border border-[#6f88b4]/18 p-2 text-center">
                <div className="text-base font-bold tabular-nums">
                  {formatCompactNumber(
                    totals.inputTokens + totals.outputTokens
                  )}
                </div>
                <div className="text-muted-foreground text-[10px]">Tokens</div>
              </div>
              <div className="bg-background/70 rounded-md border border-[#6f88b4]/18 p-2 text-center">
                <div className="text-base font-bold tabular-nums">
                  {totals.jobs}
                </div>
                <div className="text-muted-foreground text-[10px]">Jobs</div>
              </div>
              <div className="bg-background/70 rounded-md border border-[#85b85c]/18 p-2 text-center">
                <div className="text-base font-bold tabular-nums">
                  {totals.running + totals.queued + totals.retrying}
                </div>
                <div className="text-muted-foreground text-[10px]">
                  Active Now
                </div>
              </div>
            </div>

            {/* Per-model table */}
            {sortedUsageRows.length > 0 ? (
              <div className="max-h-[260px] overflow-y-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Model</TableHead>
                      <TableHead className="text-right">Status</TableHead>
                      <TableHead className="text-right">Jobs</TableHead>
                      <TableHead className="text-right">Input Tokens</TableHead>
                      <TableHead className="text-right">
                        Output Tokens
                      </TableHead>
                      <TableHead className="text-right">Cache</TableHead>
                      <TableHead className="text-right">Cost</TableHead>
                      <TableHead className="text-right">Avg Time</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {sortedUsageRows.map((row) => {
                      return (
                        <TableRow key={row.key}>
                          <TableCell>
                            <div className="flex items-center gap-2">
                              <QueueKeyIcon
                                queueKey={row.queueKey}
                                model={row.model}
                                size={12}
                              />
                              <span
                                className="font-mono text-xs font-medium"
                                title={row.model}
                              >
                                {row.model}
                              </span>
                            </div>
                          </TableCell>
                          <TableCell className="text-right">
                            <div className="flex items-center justify-end gap-1">
                              {row.running > 0 && (
                                <Badge
                                  variant="outline"
                                  className="border-[#85b85c]/30 text-[9px] font-normal text-[#5c8e43] dark:text-[#85b85c]"
                                >
                                  {row.running}
                                </Badge>
                              )}
                              {row.queued > 0 && (
                                <Badge
                                  variant="outline"
                                  className="border-[#6f88b4]/30 text-[9px] font-normal text-[#5d77a5] dark:text-[#a8b8d2]"
                                >
                                  {row.queued}
                                </Badge>
                              )}
                              {row.retrying > 0 && (
                                <Badge
                                  variant="outline"
                                  className="border-amber-500/30 text-[9px] font-normal text-amber-600 dark:text-amber-300"
                                >
                                  {row.retrying}
                                </Badge>
                              )}
                              {row.running === 0 &&
                                row.queued === 0 &&
                                row.retrying === 0 && (
                                  <span className="text-muted-foreground text-[10px]">
                                    —
                                  </span>
                                )}
                            </div>
                          </TableCell>
                          <TableCell className="text-right font-mono text-xs">
                            {row.jobCount}
                          </TableCell>
                          <TableCell className="text-right font-mono text-xs">
                            {row.hasUsageMetrics
                              ? formatCompactNumber(row.inputTokens)
                              : "—"}
                          </TableCell>
                          <TableCell className="text-right font-mono text-xs">
                            {row.hasUsageMetrics
                              ? formatCompactNumber(row.outputTokens)
                              : "—"}
                          </TableCell>
                          <TableCell className="text-muted-foreground text-right font-mono text-xs">
                            {row.hasUsageMetrics && row.cacheTokens > 0
                              ? formatCompactNumber(row.cacheTokens)
                              : "—"}
                          </TableCell>
                          <TableCell className="text-right font-mono text-xs">
                            {row.hasUsageMetrics && row.costUsd > 0
                              ? formatCost(row.costUsd)
                              : "—"}
                          </TableCell>
                          <TableCell className="text-muted-foreground text-right text-xs">
                            {row.hasUsageMetrics
                              ? formatDuration(row.avgDurationS)
                              : "—"}
                          </TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              </div>
            ) : (
              <div className="text-muted-foreground py-6 text-center text-sm">
                No job usage data yet. Worker jobs will appear here as they run.
              </div>
            )}

            {/* Totals footer */}
            {sortedUsageRows.length > 0 && (
              <div className="text-muted-foreground flex flex-wrap items-center gap-3 border-t border-[#6f88b4]/15 pt-2 text-[10px]">
                <span>
                  In: {formatCompactNumber(totals.inputTokens)} tokens
                </span>
                <span>
                  Out: {formatCompactNumber(totals.outputTokens)} tokens
                </span>
                {totals.cacheTokens > 0 && (
                  <span>Cached: {formatCompactNumber(totals.cacheTokens)}</span>
                )}
                <span className="text-foreground font-medium">
                  {formatCost(totals.cost)}
                </span>
                <span>
                  Statuses include trial and task-QA jobs; token and cost
                  metrics come from trial runs.
                </span>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

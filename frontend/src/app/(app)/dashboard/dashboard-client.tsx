"use client";

import { useDeferredValue, useEffect, useMemo, useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import Link from "next/link";
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
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type {
  DashboardExperiment,
  DashboardExperimentAuthor,
  DashboardResponse,
  ModelUsage,
  QueueStats,
} from "@/lib/types";
import { fetcher } from "@/lib/api";
import { encodeExperimentRouteParam, formatShortDateTime } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import {
  ArrowRight,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Clock,
  Copy,
  Loader2,
  Trash2,
  Globe,
  Key,
  Terminal,
} from "lucide-react";
import { QueueKeyIcon } from "@/components/queue-key-icon";

// =============================================================================
// Dashboard Hook - Single API call for all data
// =============================================================================

const EXPERIMENTS_PAGE_SIZE = 25;
const STATUS_FILTER_OPTIONS = [
  { value: "all", label: "All statuses" },
  { value: "active", label: "Active trials" },
  { value: "completed", label: "Completed" },
  { value: "needs-review", label: "Needs review" },
  { value: "pending-verdict", label: "Pending verdict" },
  { value: "failed", label: "Failures" },
] as const;

function useDashboardUsage(
  usageMinutes: number | null,
  fallbackData?: DashboardResponse | null,
) {
  const params = new URLSearchParams({
    include_tasks: "false",
    include_experiments: "false",
  });
  if (usageMinutes !== null) {
    params.set("usage_minutes", String(usageMinutes));
  }
  const swrKey = `/api/dashboard?${params.toString()}`;

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
            (Number(stats.retrying) || 0) > 0,
        );
        return hasActiveQueue ? 30000 : 90000;
      },
      revalidateOnFocus: false,
      keepPreviousData: true,
      fallbackData: fallbackData ?? undefined,
    },
  );

  return {
    health: data?.health ?? null,
    queues: data?.queues ?? null,
    pipeline: data?.pipeline ?? null,
    modelUsage: data?.model_usage ?? [],
    swrKey,
    cached: data?.cached ?? false,
    error,
    isLoading,
    isRefreshing: !error && !isLoading && isValidating,
  };
}

function useDashboardExperiments(
  experimentsLimit: number,
  experimentsOffset: number,
  experimentsQuery: string,
  experimentsStatus: string,
  fallbackData?: DashboardResponse | null,
) {
  const params = new URLSearchParams({
    experiments_limit: String(experimentsLimit),
    experiments_offset: String(experimentsOffset),
    experiments_query: experimentsQuery,
    experiments_status: experimentsStatus,
    include_tasks: "false",
    include_usage: "false",
  });
  const swrKey = `/api/dashboard?${params.toString()}`;

  const { data, error, isLoading } = useSWR<DashboardResponse>(
    swrKey,
    fetcher,
    {
      refreshInterval: 30000,
      revalidateOnFocus: false,
      keepPreviousData: true,
      fallbackData:
        experimentsOffset === 0 &&
        experimentsQuery.trim().length === 0 &&
        experimentsStatus === "all"
          ? (fallbackData ?? undefined)
          : undefined,
    },
  );

  return {
    experiments: data?.experiments ?? [],
    experimentsTotal: data?.experiments_total ?? 0,
    hasMoreExperiments: data?.experiments_has_more ?? false,
    swrKey,
    error,
    isLoading,
  };
}

function formatTaskAuthor(author: DashboardExperimentAuthor | null): string {
  if (!author) return "—";
  if (author.source === "github") {
    return `@${author.name.replace(/^@/, "")}`;
  }
  return author.name;
}

function CommandSnippet({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className="flex items-center gap-2 rounded-md border border-border/80 bg-muted/35 px-3 py-2">
      <code className="min-w-0 flex-1 overflow-x-auto font-mono text-xs">
        {command}
      </code>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="h-7 px-2 text-xs"
        onClick={handleCopy}
        aria-label="Copy command"
      >
        {copied ? (
          <Check className="h-3.5 w-3.5 text-[#5c8e43]" />
        ) : (
          <Copy className="h-3.5 w-3.5" />
        )}
      </Button>
    </div>
  );
}

function FirstRunCard() {
  return (
    <Card className="border-[#85b85c]/25 bg-card/95 shadow-sm">
      <CardContent className="flex flex-col gap-4 p-5 sm:flex-row sm:items-center sm:justify-between">
        <div className="space-y-2">
          <p className="text-sm font-medium">Set up your first Oddish run</p>
          <div className="flex flex-wrap gap-2 text-xs text-muted-foreground">
            <span className="rounded-full border border-[#85b85c]/25 bg-background/70 px-2 py-1">
              1. Install CLI
            </span>
            <span className="rounded-full border border-[#6f88b4]/25 bg-background/70 px-2 py-1">
              2. Export API key
            </span>
            <span className="rounded-full border border-[#85b85c]/25 bg-background/70 px-2 py-1">
              3. Submit job
            </span>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button asChild size="sm">
            <Link href="/settings?tab=api-keys">
              API keys
              <ArrowRight className="h-3.5 w-3.5" />
            </Link>
          </Button>
          <Button asChild variant="outline" size="sm">
            <a
              href="https://github.com/abundant-ai/oddish#quick-start"
              target="_blank"
              rel="noreferrer"
            >
              Quick start
            </a>
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function EmptyExperimentsState() {
  return (
    <div className="rounded-lg border border-dashed border-[#6f88b4]/30 bg-card/60 p-6">
      <div className="flex flex-col items-center text-center">
        <Clock className="mb-3 h-11 w-11 text-muted-foreground/70" />
        <p className="text-base font-medium">No experiments yet</p>
      </div>

      <div className="mt-5 grid gap-3 lg:grid-cols-3">
        <div className="rounded-lg border border-[#85b85c]/20 bg-background/80 p-4">
          <div className="mb-3 flex items-center gap-2 text-sm font-medium">
            <Terminal className="h-4 w-4 text-[#5c8e43]" />
            Install the CLI
          </div>
          <CommandSnippet command="uv pip install oddish" />
        </div>

        <div className="rounded-lg border border-[#6f88b4]/20 bg-background/80 p-4">
          <div className="mb-3 flex items-center gap-2 text-sm font-medium">
            <Key className="h-4 w-4 text-[#6f88b4]" />
            Add an API key
          </div>
          <CommandSnippet command={'export ODDISH_API_KEY="ok_..."'} />
        </div>

        <div className="rounded-lg border border-[#85b85c]/20 bg-background/80 p-4">
          <div className="mb-3 flex items-center gap-2 text-sm font-medium">
            <ArrowRight className="h-4 w-4 text-[#5c8e43]" />
            Submit your first job
          </div>
          <CommandSnippet command="oddish run -p my-task -a codex -m openai/gpt-5.4" />
        </div>
      </div>
    </div>
  );
}

// =============================================================================
// Usage Overview
// =============================================================================

const TIME_RANGES = [
  { key: "all", label: "All", minutes: null },
  { key: "15m", label: "15m", minutes: 15 },
  { key: "1h", label: "1h", minutes: 60 },
  { key: "6h", label: "6h", minutes: 360 },
  { key: "24h", label: "24h", minutes: 1440 },
  { key: "7d", label: "7d", minutes: 10080 },
  { key: "30d", label: "30d", minutes: 43200 },
] as const;

type PresetTimeRangeKey = (typeof TIME_RANGES)[number]["key"];
type TimeRangeKey = PresetTimeRangeKey | `custom:${number}`;

function getMinutesFromTimeRange(range: TimeRangeKey): number | null {
  if (range.startsWith("custom:")) {
    const value = Number(range.slice("custom:".length));
    return Number.isFinite(value) && value > 0 ? Math.round(value) : null;
  }
  return TIME_RANGES.find((r) => r.key === range)?.minutes ?? null;
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

function UsageOverviewCard({
  queues: _queues,
  modelUsage,
  error,
  isLoading,
  isRefreshing,
  timeRange,
  onTimeRangeChange,
}: {
  queues: QueueStats | null;
  modelUsage: ModelUsage[];
  error: Error | undefined;
  isLoading: boolean;
  isRefreshing: boolean;
  timeRange: TimeRangeKey;
  onTimeRangeChange: (key: TimeRangeKey) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [isCustomPickerOpen, setIsCustomPickerOpen] = useState(
    timeRange.startsWith("custom:"),
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

  const sortedModels = useMemo(
    () =>
      [...modelUsage].sort((a, b) => {
        const aActive = a.running + a.queued;
        const bActive = b.running + b.queued;
        if (aActive !== bActive) return bActive - aActive;
        return b.cost_usd - a.cost_usd;
      }),
    [modelUsage],
  );

  const totals = useMemo(
    () =>
      modelUsage.reduce(
        (acc, m) => ({
          trials: acc.trials + m.trial_count,
          inputTokens: acc.inputTokens + m.input_tokens,
          outputTokens: acc.outputTokens + m.output_tokens,
          cacheTokens: acc.cacheTokens + m.cache_tokens,
          cost: acc.cost + m.cost_usd,
          running: acc.running + m.running,
          queued: acc.queued + m.queued,
        }),
        {
          trials: 0,
          inputTokens: 0,
          outputTokens: 0,
          cacheTokens: 0,
          cost: 0,
          running: 0,
          queued: 0,
        },
      ),
    [modelUsage],
  );

  const selectedWindowValue = timeRange.startsWith("custom:")
    ? "custom"
    : timeRange;
  const showCustomControls =
    isCustomPickerOpen || timeRange.startsWith("custom:");

  const applyCustomWindow = () => {
    const magnitude = Number(customMagnitude);
    if (!Number.isFinite(magnitude) || magnitude <= 0) return;
    const roundedMagnitude = Math.round(magnitude);
    const minutesPerUnit =
      customUnit === "d" ? 1440 : customUnit === "h" ? 60 : 1;
    const minutes = Math.min(
      43200,
      Math.max(1, roundedMagnitude * minutesPerUnit),
    );
    onTimeRangeChange(`custom:${minutes}`);
    setIsCustomPickerOpen(false);
  };

  return (
    <Card className="border-[#6f88b4]/20 shadow-sm">
      <CardHeader className="pb-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <CardTitle className="text-base">Usage</CardTitle>
            {(isLoading || isRefreshing) && (
              <Badge
                variant="outline"
                className="text-[10px] font-normal text-muted-foreground"
              >
                <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                {isLoading ? "Loading" : "Updating"}
              </Badge>
            )}
            {totals.running > 0 && (
              <Badge
                variant="outline"
                className="border-[#85b85c]/30 text-[10px] font-normal text-[#5c8e43] dark:text-[#85b85c]"
              >
                {totals.running} running
              </Badge>
            )}
            {totals.queued > 0 && (
              <Badge
                variant="outline"
                className="border-[#6f88b4]/30 text-[10px] font-normal text-[#5d77a5] dark:text-[#a8b8d2]"
              >
                {totals.queued} queued
              </Badge>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-1">
            <select
              value={selectedWindowValue}
              onChange={(event) => {
                const value = event.target.value;
                if (value === "custom") {
                  setIsCustomPickerOpen(true);
                  return;
                }
                setIsCustomPickerOpen(false);
                onTimeRangeChange(value as PresetTimeRangeKey);
              }}
              className="h-7 w-[120px] rounded-md border border-[#6f88b4]/20 bg-background px-2 text-[11px]"
              aria-label="Time window"
            >
              {TIME_RANGES.map((range) => (
                <option key={range.key} value={range.key}>
                  {range.label}
                </option>
              ))}
              <option value="custom">Custom...</option>
            </select>
            {showCustomControls && (
              <>
                <Input
                  value={customMagnitude}
                  onChange={(event) => setCustomMagnitude(event.target.value)}
                  inputMode="numeric"
                  className="h-7 w-[66px] text-[11px]"
                  aria-label="Custom time window amount"
                />
                <select
                  value={customUnit}
                  onChange={(event) =>
                    setCustomUnit(event.target.value as "m" | "h" | "d")
                  }
                  className="h-7 w-[64px] rounded-md border border-[#6f88b4]/20 bg-background px-2 text-[11px]"
                  aria-label="Custom time window unit"
                >
                  <option value="m">min</option>
                  <option value="h">hour</option>
                  <option value="d">day</option>
                </select>
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
            <Button
              variant="ghost"
              size="sm"
              className="h-7 px-2 text-[11px] text-muted-foreground"
              onClick={() => setExpanded((v) => !v)}
              aria-expanded={expanded}
            >
              {expanded ? "Hide" : "Show"}
              <ChevronDown
                className={`ml-1 h-3.5 w-3.5 transition-transform ${
                  expanded ? "rotate-180" : ""
                }`}
              />
            </Button>
          </div>
        </div>
      </CardHeader>
      {expanded && (
        <CardContent className="space-y-3">
          {error ? (
            <Alert variant="destructive">
              <AlertTitle>Dashboard unavailable</AlertTitle>
              <AlertDescription>Failed to load usage data.</AlertDescription>
            </Alert>
          ) : isLoading && modelUsage.length === 0 ? (
            <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              Loading usage data...
            </div>
          ) : (
            <>
              {isRefreshing && (
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  Refreshing usage data...
                </div>
              )}
              {/* Summary stats row */}
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                <div className="rounded-md border border-[#6f88b4]/18 bg-background/70 p-2 text-center">
                  <div className="text-base font-bold tabular-nums">
                    {formatCost(totals.cost)}
                  </div>
                  <div className="text-[10px] text-muted-foreground">Cost</div>
                </div>
                <div className="rounded-md border border-[#6f88b4]/18 bg-background/70 p-2 text-center">
                  <div className="text-base font-bold tabular-nums">
                    {formatCompactNumber(
                      totals.inputTokens + totals.outputTokens,
                    )}
                  </div>
                  <div className="text-[10px] text-muted-foreground">
                    Tokens
                  </div>
                </div>
                <div className="rounded-md border border-[#6f88b4]/18 bg-background/70 p-2 text-center">
                  <div className="text-base font-bold tabular-nums">
                    {totals.trials}
                  </div>
                  <div className="text-[10px] text-muted-foreground">
                    Trials
                  </div>
                </div>
                <div className="rounded-md border border-[#85b85c]/18 bg-background/70 p-2 text-center">
                  <div className="text-base font-bold tabular-nums">
                    {totals.running}
                  </div>
                  <div className="text-[10px] text-muted-foreground">
                    Active Now
                  </div>
                </div>
              </div>

              {/* Per-model table */}
              {sortedModels.length > 0 ? (
                <div className="max-h-[260px] overflow-y-auto">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Model</TableHead>
                        <TableHead className="text-right">Status</TableHead>
                        <TableHead className="text-right">Trials</TableHead>
                        <TableHead className="text-right">
                          Input Tokens
                        </TableHead>
                        <TableHead className="text-right">
                          Output Tokens
                        </TableHead>
                        <TableHead className="text-right">Cache</TableHead>
                        <TableHead className="text-right">Cost</TableHead>
                        <TableHead className="text-right">Avg Time</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {sortedModels.map((m) => {
                        const queueKey = m.provider;
                        return (
                          <TableRow key={`${m.model}:${queueKey}`}>
                            <TableCell>
                              <div className="flex items-center gap-2">
                                <QueueKeyIcon
                                  queueKey={queueKey}
                                  model={m.model}
                                  size={12}
                                />
                                <span
                                  className="text-xs font-medium font-mono"
                                  title={m.model}
                                >
                                  {m.model}
                                </span>
                              </div>
                            </TableCell>
                            <TableCell className="text-right">
                              <div className="flex items-center justify-end gap-1">
                                {m.running > 0 && (
                                  <Badge
                                    variant="outline"
                                    className="border-[#85b85c]/30 text-[9px] font-normal text-[#5c8e43] dark:text-[#85b85c]"
                                  >
                                    {m.running}
                                  </Badge>
                                )}
                                {m.queued > 0 && (
                                  <Badge
                                    variant="outline"
                                    className="border-[#6f88b4]/30 text-[9px] font-normal text-[#5d77a5] dark:text-[#a8b8d2]"
                                  >
                                    {m.queued}
                                  </Badge>
                                )}
                                {m.running === 0 && m.queued === 0 && (
                                  <span className="text-[10px] text-muted-foreground">
                                    —
                                  </span>
                                )}
                              </div>
                            </TableCell>
                            <TableCell className="text-right font-mono text-xs">
                              {m.trial_count}
                            </TableCell>
                            <TableCell className="text-right font-mono text-xs">
                              {formatCompactNumber(m.input_tokens)}
                            </TableCell>
                            <TableCell className="text-right font-mono text-xs">
                              {formatCompactNumber(m.output_tokens)}
                            </TableCell>
                            <TableCell className="text-right font-mono text-xs text-muted-foreground">
                              {m.cache_tokens > 0
                                ? formatCompactNumber(m.cache_tokens)
                                : "—"}
                            </TableCell>
                            <TableCell className="text-right font-mono text-xs">
                              {m.cost_usd > 0 ? formatCost(m.cost_usd) : "—"}
                            </TableCell>
                            <TableCell className="text-right text-xs text-muted-foreground">
                              {formatDuration(m.avg_duration_s)}
                            </TableCell>
                          </TableRow>
                        );
                      })}
                    </TableBody>
                  </Table>
                </div>
              ) : (
                <div className="py-6 text-center text-sm text-muted-foreground">
                  No model usage data yet. Trials will appear here as they run.
                </div>
              )}

              {/* Totals footer */}
              {sortedModels.length > 0 && (
                <div className="flex flex-wrap items-center gap-3 border-t border-[#6f88b4]/15 pt-2 text-[10px] text-muted-foreground">
                  <span>
                    In: {formatCompactNumber(totals.inputTokens)} tokens
                  </span>
                  <span>
                    Out: {formatCompactNumber(totals.outputTokens)} tokens
                  </span>
                  {totals.cacheTokens > 0 && (
                    <span>
                      Cached: {formatCompactNumber(totals.cacheTokens)}
                    </span>
                  )}
                  <span className="font-medium text-foreground">
                    {formatCost(totals.cost)}
                  </span>
                </div>
              )}
            </>
          )}
        </CardContent>
      )}
    </Card>
  );
}

// =============================================================================
// Recent Tasks Card
// =============================================================================

function RecentTasksCard({
  experiments,
  totalExperiments,
  searchQuery,
  onSearchQueryChange,
  statusFilter,
  onStatusFilterChange,
  error,
  isLoading,
  hasMoreExperiments,
  onPreviousExperimentsPage,
  onNextExperimentsPage,
  isPageTransitioning,
  onRefreshData,
  currentExperimentsPage,
}: {
  experiments: DashboardExperiment[];
  totalExperiments: number;
  searchQuery: string;
  onSearchQueryChange: (value: string) => void;
  statusFilter: string;
  onStatusFilterChange: (value: string) => void;
  error: Error | undefined;
  isLoading: boolean;
  hasMoreExperiments: boolean;
  onPreviousExperimentsPage: () => void;
  onNextExperimentsPage: () => void;
  isPageTransitioning: boolean;
  onRefreshData: () => Promise<void>;
  currentExperimentsPage: number;
}) {
  const [deleteTarget, setDeleteTarget] = useState<{
    id: string;
    name: string;
    taskCount: number;
    totalTrials: number;
  } | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);
  const hasFilters = searchQuery.trim().length > 0 || statusFilter !== "all";
  const statusFilterLabel =
    STATUS_FILTER_OPTIONS.find((option) => option.value === statusFilter)?.label ??
    "Filter status";

  const handleDeleteExperiment = async () => {
    if (!deleteTarget || isDeleting) return;
    setIsDeleting(true);
    setDeleteError(null);

    try {
      const res = await fetch(
        `/api/experiments/${encodeExperimentRouteParam(deleteTarget.id)}`,
        { method: "DELETE" },
      );

      if (!res.ok) {
        const errorData = await res.json().catch(() => ({}));
        throw new Error(
          errorData.detail || errorData.error || "Failed to delete experiment",
        );
      }

      await onRefreshData();
      setDeleteTarget(null);
    } catch (error) {
      setDeleteError(
        error instanceof Error ? error.message : "Failed to delete experiment",
      );
    } finally {
      setIsDeleting(false);
    }
  };

  return (
    <Card className="col-span-5 border-[#6f88b4]/20 shadow-sm">
      <CardHeader className="flex flex-col gap-3 pb-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="space-y-1">
          <CardTitle className="text-base">Recent Experiments</CardTitle>
          <div className="text-[11px] text-muted-foreground">
            Showing {experiments.length} of {totalExperiments}
            {" • "}
            Page {currentExperimentsPage}
            {isPageTransitioning ? " • Loading..." : ""}
          </div>
        </div>
        <div className="flex flex-1 flex-wrap gap-2 sm:justify-end">
          <Input
            value={searchQuery}
            onChange={(event) => onSearchQueryChange(event.target.value)}
            placeholder="Search"
            className="h-8 w-full border-[#6f88b4]/20 sm:w-[220px]"
          />
          <DropdownMenu modal={false}>
            <DropdownMenuTrigger asChild>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-8 w-full justify-between border-[#6f88b4]/20 sm:w-[170px]"
              >
                <span className="truncate">{statusFilterLabel}</span>
                <ChevronDown className="h-4 w-4 opacity-50" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-[170px]">
              <DropdownMenuRadioGroup
                value={statusFilter}
                onValueChange={onStatusFilterChange}
              >
                {STATUS_FILTER_OPTIONS.map((option) => (
                  <DropdownMenuRadioItem key={option.value} value={option.value}>
                    {option.label}
                  </DropdownMenuRadioItem>
                ))}
              </DropdownMenuRadioGroup>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </CardHeader>
      <CardContent>
        {error ? (
          <Alert variant="destructive">
            <AlertTitle>Failed to load experiments</AlertTitle>
            <AlertDescription>
              Check the API connection and try again.
            </AlertDescription>
          </Alert>
        ) : isLoading && experiments.length === 0 ? (
          <p className="text-muted-foreground">Loading...</p>
        ) : !isLoading && totalExperiments === 0 && !hasFilters ? (
          <EmptyExperimentsState />
        ) : experiments.length === 0 ? (
          <div className="text-center py-8 text-muted-foreground">
            <p>No experiments match the current filters.</p>
          </div>
        ) : (
          <div className="max-h-[68vh] min-h-[560px] overflow-y-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Experiment</TableHead>
                  <TableHead>Author</TableHead>
                  <TableHead>PR</TableHead>
                  <TableHead>Tasks</TableHead>
                  <TableHead>Trials</TableHead>
                  <TableHead>Pass rate</TableHead>
                  <TableHead className="text-right">Last task</TableHead>
                  <TableHead className="text-right">Delete</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody className="[&_td]:text-xs">
                {experiments.map((experiment) => {
                  const passRate =
                    experiment.reward_total > 0
                      ? Math.round(
                          (experiment.reward_success /
                            experiment.reward_total) *
                            100,
                        )
                      : null;

                  return (
                    <TableRow key={experiment.id}>
                      <TableCell>
                        <div className="flex items-center gap-1.5">
                          <Link
                            href={`/experiments/${encodeExperimentRouteParam(
                              experiment.id,
                            )}`}
                            className="text-[#5d77a5] transition-colors hover:text-[#526a95] dark:text-[#a8b8d2] dark:hover:text-[#c0cde1]"
                          >
                            {experiment.name}
                          </Link>
                          {experiment.is_public && (
                            <Globe
                              className="h-3.5 w-3.5 text-muted-foreground"
                              aria-label="Published experiment"
                            />
                          )}
                        </div>
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground whitespace-nowrap">
                        <span className="text-foreground/80">
                          {formatTaskAuthor(experiment.last_author)}
                        </span>
                      </TableCell>
                      <TableCell className="text-xs whitespace-nowrap">
                        {experiment.last_pr_url ? (
                          <Link
                            href={experiment.last_pr_url}
                            target="_blank"
                            rel="noreferrer"
                            className="text-[#5d77a5] transition-colors hover:text-[#526a95] dark:text-[#a8b8d2] dark:hover:text-[#c0cde1]"
                          >
                            {experiment.last_pr_title
                              ? experiment.last_pr_title
                              : experiment.last_pr_number
                                ? `PR #${experiment.last_pr_number}`
                                : "PR"}
                          </Link>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </TableCell>
                      <TableCell>{experiment.task_count}</TableCell>
                      <TableCell className="font-mono text-xs whitespace-nowrap">
                        {experiment.completed_trials}/{experiment.total_trials}
                        {experiment.failed_trials > 0 && (
                          <span className="text-rose-400">
                            {" "}
                            ({experiment.failed_trials}F)
                          </span>
                        )}
                      </TableCell>
                      <TableCell className="font-mono text-xs">
                        {passRate === null ? (
                          <span className="text-muted-foreground">—</span>
                        ) : (
                          <span
                            className={
                              passRate >= 80
                                ? "text-[#5c8e43] dark:text-[#85b85c]"
                                : passRate >= 50
                                  ? "text-yellow-400"
                                  : "text-rose-400"
                            }
                          >
                            {passRate}%
                          </span>
                        )}
                      </TableCell>
                      <TableCell className="text-muted-foreground text-xs text-right whitespace-nowrap">
                        {experiment.last_created_at
                          ? formatShortDateTime(experiment.last_created_at)
                          : "—"}
                      </TableCell>
                      <TableCell className="text-right">
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() =>
                            setDeleteTarget({
                              id: experiment.id,
                              name: experiment.name,
                              taskCount: experiment.task_count,
                              totalTrials: experiment.total_trials,
                            })
                          }
                          disabled={
                            experiment.id === "uncategorized" ||
                            experiment.name === "Uncategorized"
                          }
                          className="h-8 w-8 text-destructive hover:text-destructive"
                          aria-label={`Delete ${experiment.name}`}
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
            <div className="mt-3 flex items-center gap-2">
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-8 px-3 text-[11px]"
                onClick={onPreviousExperimentsPage}
                disabled={currentExperimentsPage <= 1 || isPageTransitioning}
              >
                <ChevronLeft className="mr-1 h-3.5 w-3.5" />
                Previous page
              </Button>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-8 px-3 text-[11px]"
                onClick={onNextExperimentsPage}
                disabled={!hasMoreExperiments || isPageTransitioning}
              >
                Next page
                <ChevronRight className="ml-1 h-3.5 w-3.5" />
              </Button>
            </div>
          </div>
        )}
      </CardContent>
      <AlertDialog
        open={Boolean(deleteTarget)}
        onOpenChange={(open) => {
          if (!open) {
            setDeleteTarget(null);
            setDeleteError(null);
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete this experiment?</AlertDialogTitle>
            <AlertDialogDescription>
              This permanently deletes{" "}
              <span className="font-medium text-foreground">
                {deleteTarget?.name}
              </span>{" "}
              and removes {deleteTarget?.taskCount ?? 0} tasks and{" "}
              {deleteTarget?.totalTrials ?? 0} trials. This action cannot be
              undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          {deleteError && (
            <Alert variant="destructive">
              <AlertTitle>Delete failed</AlertTitle>
              <AlertDescription>{deleteError}</AlertDescription>
            </Alert>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={isDeleting}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleDeleteExperiment}
              disabled={isDeleting}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {isDeleting ? "Deleting..." : "Delete experiment"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Card>
  );
}

// =============================================================================
// Main Dashboard
// =============================================================================

type DashboardClientProps = {
  initialDashboardData?: DashboardResponse | null;
};

export function DashboardClient({
  initialDashboardData = null,
}: DashboardClientProps) {
  const { mutate } = useSWRConfig();
  const [experimentsOffset, setExperimentsOffset] = useState(0);
  const [searchQuery, setSearchQuery] = useState("");
  const deferredSearchQuery = useDeferredValue(searchQuery);
  const [statusFilter, setStatusFilter] = useState("all");
  const [timeRange, setTimeRange] = useState<TimeRangeKey>("24h");
  const [showRecentTasks, setShowRecentTasks] = useState(false);
  const usageMinutes = getMinutesFromTimeRange(timeRange);
  const {
    queues,
    modelUsage,
    error: usageError,
    isLoading: usageIsLoading,
    isRefreshing: usageIsRefreshing,
  } = useDashboardUsage(
    usageMinutes,
    usageMinutes === 1440 ? initialDashboardData : null,
  );
  const {
    experiments,
    experimentsTotal,
    hasMoreExperiments,
    swrKey: experimentsSwrKey,
    error: experimentsError,
    isLoading: isExperimentsLoading,
  } = useDashboardExperiments(
    EXPERIMENTS_PAGE_SIZE,
    experimentsOffset,
    deferredSearchQuery,
    statusFilter,
    deferredSearchQuery.trim().length === 0 && statusFilter === "all"
      ? initialDashboardData
      : null,
  );
  const currentExperimentsPage =
    Math.floor(experimentsOffset / EXPERIMENTS_PAGE_SIZE) + 1;

  useEffect(() => {
    setExperimentsOffset(0);
  }, [deferredSearchQuery, statusFilter]);

  const handlePreviousExperimentsPage = () => {
    setExperimentsOffset((prev) => Math.max(0, prev - EXPERIMENTS_PAGE_SIZE));
  };

  const handleNextExperimentsPage = () => {
    if (!hasMoreExperiments) return;
    setExperimentsOffset((prev) => prev + EXPERIMENTS_PAGE_SIZE);
  };

  const handleRefreshCurrentPage = async () => {
    await mutate(experimentsSwrKey);
  };

  useEffect(() => {
    const timerId = window.setTimeout(() => setShowRecentTasks(true), 0);
    return () => window.clearTimeout(timerId);
  }, []);

  return (
    <div className="space-y-4">
      {experimentsTotal === 0 && !isExperimentsLoading && !experimentsError && (
        <FirstRunCard />
      )}
      <UsageOverviewCard
        queues={queues}
        modelUsage={modelUsage}
        error={usageError}
        isLoading={usageIsLoading}
        isRefreshing={usageIsRefreshing}
        timeRange={timeRange}
        onTimeRangeChange={setTimeRange}
      />
      {showRecentTasks ? (
        <RecentTasksCard
          experiments={experiments}
          totalExperiments={experimentsTotal}
          searchQuery={searchQuery}
          onSearchQueryChange={setSearchQuery}
          statusFilter={statusFilter}
          onStatusFilterChange={setStatusFilter}
          error={experimentsError}
          isLoading={isExperimentsLoading}
          hasMoreExperiments={hasMoreExperiments}
          onPreviousExperimentsPage={handlePreviousExperimentsPage}
          onNextExperimentsPage={handleNextExperimentsPage}
          isPageTransitioning={isExperimentsLoading}
          onRefreshData={handleRefreshCurrentPage}
          currentExperimentsPage={currentExperimentsPage}
        />
      ) : (
        <Card className="col-span-5 border-[#6f88b4]/20 shadow-sm">
          <CardHeader className="pb-3">
            <CardTitle className="text-base">Recent Experiments</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-muted-foreground text-sm">Loading table…</p>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

export default DashboardClient;

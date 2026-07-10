"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip as RechartsTooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { TooltipContentProps } from "recharts";
import type {
  CostBreakdownResponse,
  CostExperimentBreakdown,
  CostModelBreakdown,
  CostSeries,
  CostUserBreakdown,
} from "@/lib/types";
import { fetcher } from "@/lib/api";
import { formatCostUsd } from "@/lib/format";
import { encodeExperimentRouteParam } from "@/lib/utils";
import { QueueKeyIcon } from "@/components/queue-key-icon";
import { AGENT_COLORS } from "@/components/pass-at-k-graph";
import { AlertCircle, DollarSign, Info, RefreshCw } from "lucide-react";

// window_days values the backend understands (0 == all-time).
const WINDOW_OPTIONS: { value: string; label: string }[] = [
  { value: "1", label: "Last 24 hours" },
  { value: "7", label: "Last 7 days" },
  { value: "30", label: "Last 30 days" },
  { value: "90", label: "Last 90 days" },
  { value: "0", label: "All time" },
];

// The backend folds everything beyond the top-N into this synthetic key.
const OTHER_KEY = "__other__";
const OTHER_COLOR = "#94a3b8"; // slate-400 — neutral for the folded "Other".

// Reuse the app's shared agent/model palette (the pass@k chart / leaderboard
// use the same one). Colors are assigned deterministically per key name so a
// given model/user keeps the same hue across renders and dimensions — rather
// than by stack position, which made the color depend on rank. Greedy
// collision avoidance keeps adjacent segments distinct within a chart.
function hashKey(key: string): number {
  let hash = 0;
  for (let i = 0; i < key.length; i += 1) {
    hash = (hash * 31 + key.charCodeAt(i)) >>> 0;
  }
  return hash;
}

function buildSeriesColors(keys: { key: string }[]): Record<string, string> {
  const result: Record<string, string> = {};
  const used = new Set<number>();
  const pending: string[] = [];
  for (const { key } of keys) {
    if (key === OTHER_KEY) {
      result[key] = OTHER_COLOR;
      continue;
    }
    const pref = hashKey(key) % AGENT_COLORS.length;
    if (used.has(pref)) {
      pending.push(key);
    } else {
      used.add(pref);
      result[key] = AGENT_COLORS[pref];
    }
  }
  for (const key of pending) {
    let slot = hashKey(key) % AGENT_COLORS.length;
    if (used.size < AGENT_COLORS.length) {
      for (let step = 0; step < AGENT_COLORS.length; step += 1) {
        const candidate = (slot + step) % AGENT_COLORS.length;
        if (!used.has(candidate)) {
          slot = candidate;
          break;
        }
      }
    }
    used.add(slot);
    result[key] = AGENT_COLORS[slot];
  }
  return result;
}

function formatTokens(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0";
  if (value >= 1e9) return `${(value / 1e9).toFixed(1)}B`;
  if (value >= 1e6) return `${(value / 1e6).toFixed(1)}M`;
  if (value >= 1e3) return `${(value / 1e3).toFixed(1)}k`;
  return `${value}`;
}

function estimatedPct(cost: number, estimated: number): number {
  if (cost <= 0) return 0;
  return Math.round((estimated / cost) * 100);
}

function formatSignedCost(value: number): string {
  const sign = value >= 0 ? "+" : "−";
  return `${sign}${formatCostUsd(Math.abs(value))}`;
}

// Signed cost delta vs the prior window; null render when there is no prior
// figure (all-time). A rise is bad (red), a drop good (green).
function DeltaBadge({
  current,
  prev,
}: {
  current: number;
  prev: number | null | undefined;
}) {
  if (prev == null) return null;
  const diff = current - prev;
  const pct =
    prev === 0
      ? current > 0
        ? "new"
        : null
      : `${diff >= 0 ? "+" : "−"}${Math.abs(Math.round((diff / prev) * 100))}%`;
  const color =
    diff === 0
      ? "text-muted-foreground"
      : diff > 0
        ? "text-destructive"
        : "text-[#5c8e43] dark:text-[#85b85c]";
  return (
    <span className={`text-[10px] font-medium tabular-nums ${color}`}>
      {formatSignedCost(diff)}
      {pct ? ` · ${pct}` : ""}
    </span>
  );
}

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

function modelTasksHref(model: string, windowDays: string): string {
  const params = new URLSearchParams({
    models: model,
    sort: "cost_desc",
  });
  const trialFinishedWithin =
    windowDays === "1"
      ? "24h"
      : ["7", "30", "90"].includes(windowDays)
        ? `${windowDays}d`
        : null;
  if (trialFinishedWithin) {
    params.set("trial_finished_within", trialFinishedWithin);
  }
  return `/tasks?${params.toString()}`;
}

function ModelLabel({
  model,
  windowDays,
}: {
  model: CostModelBreakdown;
  windowDays: string;
}) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <QueueKeyIcon model={model.model} size={13} />
      <Link
        href={modelTasksHref(model.model, windowDays)}
        className="font-mono text-[11px] text-[#5d77a5] hover:underline dark:text-[#a8b8d2]"
      >
        {model.model}
      </Link>
      <span className="text-muted-foreground text-[10px]">
        {model.provider}
      </span>
    </span>
  );
}

// Top model inline + "+N more" with a tooltip listing every model's cost.
function ModelMix({ models }: { models: CostModelBreakdown[] }) {
  if (models.length === 0)
    return <span className="text-muted-foreground">—</span>;
  const [top, ...rest] = models;
  return (
    <span className="inline-flex items-center gap-1.5">
      <QueueKeyIcon model={top.model} size={13} />
      <span className="font-mono text-[11px]">{top.model}</span>
      {rest.length > 0 && (
        <Tooltip>
          <TooltipTrigger asChild>
            <Badge variant="outline" className="cursor-help text-[10px]">
              +{rest.length}
            </Badge>
          </TooltipTrigger>
          <TooltipContent className="max-w-[320px]">
            <div className="space-y-0.5">
              {models.map((m) => (
                <div
                  key={`${m.model}-${m.provider}`}
                  className="flex justify-between gap-4 font-mono text-[11px]"
                >
                  <span>
                    {m.model}{" "}
                    <span className="text-muted-foreground">{m.provider}</span>
                  </span>
                  <span>{formatCostUsd(m.cost_usd)}</span>
                </div>
              ))}
            </div>
          </TooltipContent>
        </Tooltip>
      )}
    </span>
  );
}

function userLabel(user: CostUserBreakdown): string {
  // Synthetic fallback rows (GitHub handle / Unattributed) always carry a
  // `label`, so `key` is only reached for a real-user row (billed/submitter)
  // whose name/email enrichment came back empty — the user id beats a bare
  // "Unattributed" there, and a synthetic key never leaks through.
  return (
    user.label ||
    user.name ||
    user.email ||
    user.owner_user_id ||
    user.key ||
    "Unattributed"
  );
}

// Small inline marker shown next to a cost when part of it was estimated from
// tokens (no native runtime cost). Replaces the old dedicated "Est" column.
function EstimateMarker({
  cost,
  estimated,
}: {
  cost: number;
  estimated: number;
}) {
  if (estimated <= 0) return null;
  const pct = estimatedPct(cost, estimated);
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Info className="text-muted-foreground ml-1 inline h-3 w-3 cursor-help align-text-top" />
      </TooltipTrigger>
      <TooltipContent className="max-w-[280px]">
        {formatCostUsd(estimated)} of {formatCostUsd(cost)} ({pct}%) is
        estimated from token counts (no native cost was reported by the
        runtime); the rest is the runtime-reported cost.
      </TooltipContent>
    </Tooltip>
  );
}

function CostCell({ cost, estimated }: { cost: number; estimated: number }) {
  return (
    <TableCell className="text-right font-mono text-xs font-medium">
      <span className="inline-flex items-center justify-end">
        {formatCostUsd(cost)}
        <EstimateMarker cost={cost} estimated={estimated} />
      </span>
    </TableCell>
  );
}

// =============================================================================
// Cost-over-time chart (stacked by model or by user)
// =============================================================================

function bucketTickFormatter(bucket: string) {
  return (value: string) => {
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return value;
    if (bucket === "hour")
      return d.toLocaleTimeString(undefined, {
        hour: "numeric",
        hour12: true,
      });
    return d.toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
      timeZone: "UTC",
    });
  };
}

type ChartTooltipValue = number | string | ReadonlyArray<number | string>;
type ChartTooltipName = number | string;

function ChartTooltip(
  props: TooltipContentProps<ChartTooltipValue, ChartTooltipName> & {
    bucket: string;
    labels: Record<string, string>;
  }
) {
  const { active, payload, label, bucket, labels } = props;
  if (!active || !payload || payload.length === 0) return null;
  const when =
    bucket === "hour"
      ? new Date(String(label)).toLocaleString()
      : new Date(String(label)).toLocaleDateString(undefined, {
          timeZone: "UTC",
        });
  const entries = payload
    .map((p) => ({
      key: String(p.dataKey),
      color: p.color,
      value: typeof p.value === "number" ? p.value : 0,
    }))
    .filter((e) => e.value > 0)
    .sort((a, b) => b.value - a.value);
  const total = entries.reduce((sum, e) => sum + e.value, 0);
  return (
    <div className="bg-popover max-w-[280px] rounded-md border px-3 py-2 text-xs shadow-md">
      <div className="mb-1 font-medium">{when}</div>
      <div className="space-y-0.5">
        {entries.map((e) => (
          <div key={e.key} className="flex items-center justify-between gap-3">
            <span className="flex items-center gap-1.5">
              <span
                className="inline-block h-2 w-2 rounded-sm"
                style={{ backgroundColor: e.color }}
              />
              <span className="max-w-[160px] truncate">
                {labels[e.key] ?? e.key}
              </span>
            </span>
            <span className="font-mono">{formatCostUsd(e.value)}</span>
          </div>
        ))}
      </div>
      <div className="mt-1 flex justify-between gap-3 border-t pt-1 font-medium">
        <span>Total</span>
        <span className="font-mono">{formatCostUsd(total)}</span>
      </div>
    </div>
  );
}

export function CostChart({
  series,
  bucket,
}: {
  series: CostSeries;
  bucket: string;
}) {
  const labels = useMemo(() => {
    const map: Record<string, string> = {};
    series.keys.forEach((k) => (map[k.key] = k.label));
    return map;
  }, [series.keys]);

  const data = useMemo(
    () =>
      series.buckets.map((b) => ({
        bucket_start: b.bucket_start,
        ...Object.fromEntries(
          series.keys.map((k) => [k.key, b.costs[k.key] ?? 0])
        ),
      })),
    [series.buckets, series.keys]
  );

  const colorByKey = useMemo(
    () => buildSeriesColors(series.keys),
    [series.keys]
  );

  if (series.buckets.length === 0)
    return (
      <div className="text-muted-foreground flex h-[240px] items-center justify-center rounded-lg border text-sm">
        No trial spend in this window.
      </div>
    );

  return (
    <div className="space-y-2">
      <div className="h-[240px] w-full">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={data}
            margin={{ top: 8, right: 8, bottom: 0, left: 8 }}
          >
            <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
            <XAxis
              dataKey="bucket_start"
              tickFormatter={bucketTickFormatter(bucket)}
              tick={{ fontSize: 11 }}
              minTickGap={24}
            />
            <YAxis
              tickFormatter={(v: number) => formatCostUsd(v)}
              tick={{ fontSize: 11 }}
              width={56}
            />
            <RechartsTooltip
              content={(props) => (
                <ChartTooltip {...props} bucket={bucket} labels={labels} />
              )}
              cursor={{ fill: "var(--muted)", opacity: 0.3 }}
            />
            {series.keys.map((k, i) => (
              <Bar
                key={k.key}
                dataKey={k.key}
                stackId="cost"
                fill={colorByKey[k.key]}
                name={k.label}
                radius={i === series.keys.length - 1 ? [2, 2, 0, 0] : undefined}
              />
            ))}
          </BarChart>
        </ResponsiveContainer>
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px]">
        {series.keys.map((k) => (
          <span key={k.key} className="inline-flex items-center gap-1">
            <span
              className="inline-block h-2 w-2 rounded-sm"
              style={{ backgroundColor: colorByKey[k.key] }}
            />
            <span className="text-muted-foreground max-w-[160px] truncate">
              {k.label}
            </span>
          </span>
        ))}
      </div>
    </div>
  );
}

// =============================================================================
// Methodology note
// =============================================================================

function MethodologyNote() {
  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="h-6 w-6"
          aria-label="How costs are computed"
        >
          <Info className="h-4 w-4" />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className="w-[360px] text-xs leading-relaxed"
      >
        <p className="mb-2 font-medium">How costs are computed</p>
        <ul className="text-muted-foreground list-disc space-y-1.5 pl-4">
          <li>
            Per-trial cost: the runtime&apos;s native cost, else a token-based
            estimate (LiteLLM pricing). The{" "}
            <Info className="inline h-3 w-3 align-text-top" /> marker flags a
            cost that was estimated.
          </li>
          <li>
            All first-party spend, across every org. Imported runs and
            experiment-combine copies are excluded so spend counts once.
          </li>
          <li>
            Deleted trials, tasks, and experiments remain included because
            deleting a record does not erase historical spend. A badge marks
            affected experiment rows.
          </li>
          <li>
            Per-user attributes each trial to its billed user; spend with no
            active payer falls to its GitHub identity, submitter, or an{" "}
            <strong>unbilled</strong> &ldquo;Unattributed&rdquo; bucket.
            Per-model and per-user regroup the same costs, so every view sums to
            the same total.
          </li>
        </ul>
      </PopoverContent>
    </Popover>
  );
}

// =============================================================================
// Top-level card
// =============================================================================

type ChartDimension = "agent" | "model" | "user";

const CHART_DIMENSIONS: ChartDimension[] = ["agent", "model", "user"];

export function CostBreakdownCard() {
  const [windowDays, setWindowDays] = useState("1");
  const [dimension, setDimension] = useState<ChartDimension>("agent");

  const { data, error, isLoading, mutate } = useSWR<CostBreakdownResponse>(
    `/api/admin/costs?window_days=${windowDays}&experiment_limit=100&user_limit=100`,
    fetcher,
    { refreshInterval: 30000 }
  );

  const windowLabel =
    WINDOW_OPTIONS.find((o) => o.value === windowDays)?.label ?? windowDays;
  const series = data
    ? dimension === "agent"
      ? data.series_by_agent
      : dimension === "model"
        ? data.series_by_model
        : data.series_by_user
    : null;

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <DollarSign className="h-5 w-5" />
            <CardTitle className="text-base">Cost Breakdown</CardTitle>
            <MethodologyNote />
          </div>
          <div className="flex items-center gap-2">
            <Select value={windowDays} onValueChange={setWindowDays}>
              <SelectTrigger className="h-8 w-[150px] text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {WINDOW_OPTIONS.map((opt) => (
                  <SelectItem key={opt.value} value={opt.value}>
                    {opt.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {data && (
              <span className="text-muted-foreground hidden text-[10px] sm:inline">
                Updated {new Date(data.timestamp).toLocaleTimeString()}
              </span>
            )}
            <Button
              variant="outline"
              size="icon"
              className="h-8 w-8"
              onClick={() => mutate()}
              disabled={isLoading}
              aria-label="Refresh cost breakdown"
            >
              <RefreshCw
                className={`h-4 w-4 ${isLoading ? "animate-spin" : ""}`}
              />
            </Button>
          </div>
        </div>
        <p className="text-muted-foreground text-xs">
          All first-party trial spend across all organizations, including
          deleted historical spend and unbilled spend that never resolved to a
          registered user — see the info icon for methodology.
        </p>
      </CardHeader>
      <CardContent className="space-y-6">
        {error ? (
          <Alert variant="destructive">
            <AlertCircle className="h-4 w-4" />
            <AlertTitle>Failed to load cost breakdown</AlertTitle>
            <AlertDescription>
              {error instanceof Error
                ? error.message
                : "Check if you have admin access."}
            </AlertDescription>
          </Alert>
        ) : !data || !series ? (
          <p className="text-muted-foreground">Loading...</p>
        ) : (
          <TooltipProvider delayDuration={150}>
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-medium">Cost over time</h3>
                <div className="flex items-center gap-1 text-xs">
                  <span className="text-muted-foreground mr-1">stack by</span>
                  {CHART_DIMENSIONS.map((dim) => (
                    <Button
                      key={dim}
                      variant={dimension === dim ? "secondary" : "ghost"}
                      size="sm"
                      className="h-7 px-2 text-xs capitalize"
                      onClick={() => setDimension(dim)}
                    >
                      {dim}
                    </Button>
                  ))}
                </div>
              </div>
              <CostChart series={series} bucket={data.bucket} />
            </div>

            <StatTiles totals={data.totals} />

            <section className="space-y-2">
              <h3 className="text-sm font-medium">Cost by user</h3>
              <UserTable users={data.by_user} windowDays={windowDays} />
            </section>

            <section className="space-y-2">
              <h3 className="text-sm font-medium">Cost by model</h3>
              <ModelTable models={data.by_model} windowDays={windowDays} />
            </section>

            <section className="space-y-2">
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-medium">Top experiments by cost</h3>
                <span className="text-muted-foreground text-[11px]">
                  ranked descending · {windowLabel.toLowerCase()}
                </span>
              </div>
              <ExperimentTable experiments={data.experiments} />
            </section>
          </TooltipProvider>
        )}
      </CardContent>
    </Card>
  );
}

function StatTiles({ totals }: { totals: CostBreakdownResponse["totals"] }) {
  const prev = totals.prev_cost_usd;
  const diff = prev == null ? null : totals.cost_usd - prev;
  const pctLabel =
    prev == null || diff == null
      ? null
      : prev === 0
        ? totals.cost_usd > 0
          ? "new"
          : null
        : `${diff >= 0 ? "+" : "−"}${Math.abs(Math.round((diff / prev) * 100))}%`;
  const monthCost = totals.month_cost_usd ?? 0;
  const budget = totals.month_budget_usd;
  const monthPct =
    budget != null && budget > 0 ? (monthCost / budget) * 100 : null;
  const monthFill =
    monthPct != null && monthPct >= 80
      ? "bg-destructive"
      : monthPct != null && monthPct >= 60
        ? "bg-amber-500"
        : "bg-blue-500";
  return (
    <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
      <div className="bg-background/70 rounded-md border border-[#6f88b4]/18 p-2 text-center">
        <div className="text-base font-bold tabular-nums">
          {formatCostUsd(totals.cost_usd)}
        </div>
        <div className="text-muted-foreground text-[10px]">Total cost</div>
      </div>
      <div className="bg-background/70 rounded-md border border-[#6f88b4]/18 p-2 text-center">
        <div className="text-base font-bold tabular-nums">
          {formatCostUsd(monthCost)}
          {budget != null && (
            <span className="text-muted-foreground font-normal">
              {" "}
              / {formatCostUsd(budget)}
            </span>
          )}
        </div>
        <div className="text-muted-foreground text-[10px]">
          Monthly quota
          {monthPct != null
            ? ` · ${Math.round(monthPct)}%`
            : " · month to date"}
        </div>
        {monthPct != null && (
          <div className="bg-muted-foreground/20 mx-auto mt-1 h-1.5 w-24 overflow-hidden rounded-full">
            <div
              className={`h-full ${monthFill}`}
              style={{ width: `${Math.min(monthPct, 100)}%` }}
            />
          </div>
        )}
      </div>
      <div className="bg-background/70 rounded-md border border-[#6f88b4]/18 p-2 text-center">
        {diff == null ? (
          <div className="text-muted-foreground text-base font-bold">—</div>
        ) : (
          <div
            className={`text-base font-bold tabular-nums ${
              diff > 0
                ? "text-destructive"
                : diff < 0
                  ? "text-[#5c8e43] dark:text-[#85b85c]"
                  : ""
            }`}
          >
            {formatSignedCost(diff)}
            {pctLabel ? (
              <span className="text-muted-foreground font-normal">
                {" "}
                · {pctLabel}
              </span>
            ) : null}
          </div>
        )}
        <div className="text-muted-foreground text-[10px]">
          Δ vs prior period
        </div>
      </div>
    </div>
  );
}

function QuotaCell({ user }: { user: CostUserBreakdown }) {
  const spent = user.quota_spent_usd;
  const limit = user.quota_limit_usd;
  if (spent == null || limit == null)
    return (
      <TableCell className="text-muted-foreground text-right text-xs">
        —
      </TableCell>
    );
  const pct = limit > 0 ? (spent / limit) * 100 : spent > 0 ? 100 : 0;
  const clamped = Math.min(pct, 100);
  const fill =
    pct >= 80 ? "bg-destructive" : pct >= 60 ? "bg-amber-500" : "bg-blue-500";
  return (
    <TableCell className="text-right">
      <div className="flex flex-col items-end gap-1">
        <span className="font-mono text-[11px]">
          {formatCostUsd(spent)} / {formatCostUsd(limit)}
          <span
            className={`ml-1 ${pct >= 100 ? "text-destructive font-bold" : "text-muted-foreground"}`}
          >
            {Math.round(pct)}%
          </span>
        </span>
        <div className="bg-muted-foreground/20 h-1.5 w-20 overflow-hidden rounded-full">
          <div className={`h-full ${fill}`} style={{ width: `${clamped}%` }} />
        </div>
      </div>
    </TableCell>
  );
}

function RunningCell({ count }: { count: number }) {
  if (count <= 0)
    return (
      <TableCell className="text-muted-foreground text-right text-xs">
        —
      </TableCell>
    );
  return (
    <TableCell className="text-right">
      <Badge variant="outline" className="gap-1 text-[10px]">
        <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-blue-500" />
        {count.toLocaleString()}
      </Badge>
    </TableCell>
  );
}

function UserTable({
  users,
  windowDays,
}: {
  users: CostUserBreakdown[];
  windowDays: string;
}) {
  if (users.length === 0)
    return (
      <p className="text-muted-foreground py-3 text-xs">
        No trial spend in this window.
      </p>
    );
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>User</TableHead>
          <TableHead>Org</TableHead>
          <TableHead className="text-right">Cost</TableHead>
          <TableHead className="text-right">Δ prior</TableHead>
          <TableHead className="text-right">
            <Tooltip>
              <TooltipTrigger asChild>
                <span className="cursor-help">24h quota</span>
              </TooltipTrigger>
              <TooltipContent className="max-w-[280px]">
                Rolling-24h settled spend vs quota limit — the enforcement
                basis, so it will not match the window cost column.
              </TooltipContent>
            </Tooltip>
          </TableHead>
          <TableHead className="text-right">Running</TableHead>
          <TableHead className="text-right">Trials</TableHead>
          <TableHead className="text-right">Exps</TableHead>
          <TableHead>Top models</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {users.map((user) => (
          <TableRow key={`${user.org_id ?? "-"}:${user.key}`}>
            <TableCell>
              <div className="flex flex-col">
                {user.owner_user_id ? (
                  <span className="inline-flex items-center gap-1.5 text-xs font-medium">
                    <Link
                      href={`/admin/users/${encodeURIComponent(user.owner_user_id)}?window_days=${encodeURIComponent(windowDays)}${
                        user.org_id
                          ? `&org=${encodeURIComponent(user.org_id)}`
                          : ""
                      }`}
                      className="text-[#5d77a5] hover:underline dark:text-[#a8b8d2]"
                    >
                      {userLabel(user)}
                    </Link>
                    {user.has_unbilled_spend && (
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <Badge
                            variant="outline"
                            className="text-muted-foreground cursor-help text-[9px] font-normal"
                          >
                            unbilled
                          </Badge>
                        </TooltipTrigger>
                        <TooltipContent className="max-w-[260px]">
                          This user has unbilled trials — spend that never drew
                          a quota (e.g. created before billing stamping shipped,
                          or while they were unlinked). Their drilldown counts
                          billed spend only, so its total may be less than the
                          amount shown here.
                        </TooltipContent>
                      </Tooltip>
                    )}
                  </span>
                ) : (
                  <span className="inline-flex items-center gap-1.5 text-xs font-medium">
                    {userLabel(user)}
                    {user.has_unbilled_spend && (
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <Badge
                            variant="outline"
                            className="text-muted-foreground cursor-help text-[9px] font-normal"
                          >
                            unbilled
                          </Badge>
                        </TooltipTrigger>
                        <TooltipContent className="max-w-[260px]">
                          This spender isn&apos;t a registered oddish user, so
                          this cost isn&apos;t billed to anyone&apos;s quota.
                        </TooltipContent>
                      </Tooltip>
                    )}
                  </span>
                )}
                {user.email && user.email !== userLabel(user) && (
                  <span className="text-muted-foreground text-[10px]">
                    {user.email}
                  </span>
                )}
              </div>
            </TableCell>
            <TableCell className="text-muted-foreground text-[11px]">
              {user.org_name ?? user.org_id ?? "—"}
            </TableCell>
            <CostCell
              cost={user.cost_usd}
              estimated={user.cost_estimated_usd}
            />
            <TableCell className="text-right">
              <DeltaBadge current={user.cost_usd} prev={user.prev_cost_usd} />
            </TableCell>
            <QuotaCell user={user} />
            <RunningCell count={user.inflight_trial_count ?? 0} />
            <TableCell className="text-right font-mono text-xs">
              {user.trial_count.toLocaleString()}
            </TableCell>
            <TableCell className="text-right font-mono text-xs">
              {user.experiment_count.toLocaleString()}
            </TableCell>
            <TableCell>
              <ModelMix models={user.models} />
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

function ModelTable({
  models,
  windowDays,
}: {
  models: CostModelBreakdown[];
  windowDays: string;
}) {
  if (models.length === 0)
    return (
      <p className="text-muted-foreground py-3 text-xs">
        No model usage in this window.
      </p>
    );
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Model</TableHead>
          <TableHead className="text-right">Cost</TableHead>
          <TableHead className="text-right">Trials</TableHead>
          <TableHead className="text-right">Input</TableHead>
          <TableHead className="text-right">Output</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {models.map((model) => (
          <TableRow key={`${model.model}-${model.provider}`}>
            <TableCell>
              <ModelLabel model={model} windowDays={windowDays} />
            </TableCell>
            <CostCell
              cost={model.cost_usd}
              estimated={model.cost_estimated_usd}
            />
            <TableCell className="text-right font-mono text-xs">
              {model.trial_count.toLocaleString()}
            </TableCell>
            <TableCell className="text-right font-mono text-xs">
              {formatTokens(model.input_tokens)}
            </TableCell>
            <TableCell className="text-right font-mono text-xs">
              {formatTokens(model.output_tokens)}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

function DeletedSpendBadge({
  isDeleted,
  hasDeletedSpend,
}: {
  isDeleted: boolean;
  hasDeletedSpend: boolean;
}) {
  if (!hasDeletedSpend) return null;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge
          variant="outline"
          className="text-muted-foreground cursor-help text-[9px] font-normal"
        >
          {isDeleted ? "deleted" : "includes deleted"}
        </Badge>
      </TooltipTrigger>
      <TooltipContent className="max-w-[280px]">
        {isDeleted
          ? "This experiment was deleted, but its historical spend still counts."
          : "This experiment includes spend from deleted trials or tasks."}
      </TooltipContent>
    </Tooltip>
  );
}

function ExperimentTable({
  experiments,
}: {
  experiments: CostExperimentBreakdown[];
}) {
  if (experiments.length === 0)
    return (
      <p className="text-muted-foreground py-3 text-xs">
        No experiments with trial spend in this window.
      </p>
    );
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Experiment</TableHead>
          <TableHead>Owner</TableHead>
          <TableHead className="text-right">
            <span className="inline-flex items-center gap-1">
              New spend
              <Tooltip>
                <TooltipTrigger asChild>
                  <Info className="h-3 w-3 cursor-help" />
                </TooltipTrigger>
                <TooltipContent className="max-w-[260px]">
                  Cost from all trials run during the analysis period, not full
                  experiment cost.
                </TooltipContent>
              </Tooltip>
            </span>
          </TableHead>
          <TableHead className="text-right">Trials</TableHead>
          <TableHead>Models</TableHead>
          <TableHead className="text-right">Activity</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {experiments.map((exp) => (
          <TableRow key={exp.experiment_id}>
            <TableCell className="max-w-[220px]">
              <span className="inline-flex items-center gap-1.5">
                {exp.is_deleted ? (
                  <span
                    className="truncate text-xs font-medium"
                    title={exp.name ?? exp.experiment_id}
                  >
                    {exp.name ?? exp.experiment_id}
                  </span>
                ) : (
                  <Link
                    href={`/experiments/${encodeExperimentRouteParam(exp.experiment_id)}`}
                    className="truncate text-xs font-medium text-[#5d77a5] hover:underline dark:text-[#a8b8d2]"
                    title={exp.name ?? exp.experiment_id}
                  >
                    {exp.name ?? exp.experiment_id}
                  </Link>
                )}
                <DeletedSpendBadge
                  isDeleted={exp.is_deleted}
                  hasDeletedSpend={exp.has_deleted_spend}
                />
              </span>
            </TableCell>
            <TableCell className="text-muted-foreground text-[11px]">
              {exp.owner_name ?? exp.owner_email ?? exp.owner_label ?? "—"}
            </TableCell>
            <CostCell cost={exp.cost_usd} estimated={exp.cost_estimated_usd} />
            <TableCell className="text-right font-mono text-xs">
              {exp.trial_count.toLocaleString()}
            </TableCell>
            <TableCell>
              <ModelMix models={exp.models} />
            </TableCell>
            <TableCell className="text-muted-foreground text-right text-[11px]">
              {formatAge(exp.last_activity_at)}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

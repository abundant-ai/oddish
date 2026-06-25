"use client";

import { useState } from "react";
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
  CostSeriesPoint,
  CostUserBreakdown,
} from "@/lib/types";
import { fetcher } from "@/lib/api";
import { formatCostUsd } from "@/lib/format";
import { encodeExperimentRouteParam } from "@/lib/utils";
import { QueueKeyIcon } from "@/components/queue-key-icon";
import { AlertCircle, DollarSign, Info, RefreshCw } from "lucide-react";

// window_days values the backend understands (0 == all-time).
const WINDOW_OPTIONS: { value: string; label: string }[] = [
  { value: "1", label: "Last 24 hours" },
  { value: "7", label: "Last 7 days" },
  { value: "30", label: "Last 30 days" },
  { value: "90", label: "Last 90 days" },
  { value: "0", label: "All time" },
];

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

function ModelLabel({ model }: { model: CostModelBreakdown }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <QueueKeyIcon model={model.model} size={13} />
      <span className="font-mono text-[11px]">{model.model}</span>
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
  return user.name || user.email || user.owner_user_id || "Unattributed";
}

function EstimatedBadge({
  cost,
  estimated,
}: {
  cost: number;
  estimated: number;
}) {
  const pct = estimatedPct(cost, estimated);
  if (pct <= 0) return <span className="text-muted-foreground">—</span>;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge variant="outline" className="cursor-help text-[10px]">
          {pct}% est
        </Badge>
      </TooltipTrigger>
      <TooltipContent className="max-w-[280px]">
        {formatCostUsd(estimated)} of {formatCostUsd(cost)} is estimated from
        token counts (no native cost was reported by the runtime); the rest is
        the runtime-reported cost.
      </TooltipContent>
    </Tooltip>
  );
}

// =============================================================================
// Cost-over-time chart
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
    if (bucket === "week" || bucket === "day")
      return d.toLocaleDateString(undefined, {
        month: "short",
        day: "numeric",
      });
    return d.toLocaleDateString();
  };
}

type ChartTooltipValue = number | string | ReadonlyArray<number | string>;
type ChartTooltipName = number | string;

function ChartTooltip(
  props: TooltipContentProps<ChartTooltipValue, ChartTooltipName> & {
    bucket: string;
  }
) {
  const { active, payload, bucket } = props;
  if (!active || !payload || payload.length === 0) return null;
  const point = payload[0].payload as CostSeriesPoint;
  const label =
    bucket === "hour"
      ? new Date(point.bucket_start).toLocaleString()
      : new Date(point.bucket_start).toLocaleDateString();
  return (
    <div className="bg-popover rounded-md border px-3 py-2 text-xs shadow-md">
      <div className="mb-1 font-medium">{label}</div>
      <div className="flex justify-between gap-4">
        <span className="text-muted-foreground">Total</span>
        <span className="font-mono">{formatCostUsd(point.cost_usd)}</span>
      </div>
      <div className="flex justify-between gap-4">
        <span className="text-muted-foreground">Native</span>
        <span className="font-mono">
          {formatCostUsd(point.cost_native_usd)}
        </span>
      </div>
      <div className="flex justify-between gap-4">
        <span className="text-muted-foreground">Estimated</span>
        <span className="font-mono">
          {formatCostUsd(point.cost_estimated_usd)}
        </span>
      </div>
      <div className="text-muted-foreground mt-1">
        {point.trial_count.toLocaleString()} trials
      </div>
    </div>
  );
}

function CostChart({
  series,
  bucket,
}: {
  series: CostSeriesPoint[];
  bucket: string;
}) {
  if (series.length === 0)
    return (
      <div className="text-muted-foreground flex h-[220px] items-center justify-center rounded-lg border text-sm">
        No trial spend in this window.
      </div>
    );
  return (
    <div className="h-[220px] w-full">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart
          data={series}
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
            content={(props) => <ChartTooltip {...props} bucket={bucket} />}
            cursor={{ fill: "var(--muted)", opacity: 0.3 }}
          />
          {/* Native + estimated stacked so the split is visible over time. */}
          <Bar
            dataKey="cost_native_usd"
            stackId="cost"
            fill="#3b82f6"
            name="Native"
          />
          <Bar
            dataKey="cost_estimated_usd"
            stackId="cost"
            fill="#f59e0b"
            name="Estimated"
            radius={[2, 2, 0, 0]}
          />
        </BarChart>
      </ResponsiveContainer>
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
            Cost is tallied <strong>per trial</strong>. When the agent runtime
            reports a cost we use that (<strong>native</strong>); otherwise we
            estimate it from the trial&apos;s token counts × per-model pricing
            (LiteLLM&apos;s table plus a small local fallback) —{" "}
            <strong>estimated</strong>. This is the same estimator the rest of
            the app already uses for per-trial cost.
          </li>
          <li>
            Native and estimated are mutually exclusive per trial, so they sum
            to the total with no double-counting. A low estimated share just
            means most trials&apos; runtimes reported a cost (e.g. Claude Code);
            agents that report tokens but not cost get estimated.
          </li>
          <li>
            Per-user figures attribute each experiment to its owner; per-model
            and per-user are the same per-trial costs grouped differently, so
            each view sums back to the same total.
          </li>
          <li>Figures span all organizations.</li>
        </ul>
      </PopoverContent>
    </Popover>
  );
}

// =============================================================================
// Top-level card
// =============================================================================

export function CostBreakdownCard() {
  const [windowDays, setWindowDays] = useState("7");

  const { data, error, isLoading, mutate } = useSWR<CostBreakdownResponse>(
    `/api/admin/costs?window_days=${windowDays}&experiment_limit=100&user_limit=100`,
    fetcher,
    { refreshInterval: 30000 }
  );

  const windowLabel =
    WINDOW_OPTIONS.find((o) => o.value === windowDays)?.label ?? windowDays;

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <DollarSign className="h-5 w-5" />
            <CardTitle className="text-base">Cost Breakdown</CardTitle>
            <MethodologyNote />
            {data && (
              <Badge variant="outline" className="text-xs">
                {formatCostUsd(data.totals.cost_usd)} ·{" "}
                {windowLabel.toLowerCase()}
              </Badge>
            )}
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
          Total trial spend across all organizations. Native runtime cost when
          reported, otherwise a per-model token estimate — see the info icon for
          methodology.
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
        ) : !data ? (
          <p className="text-muted-foreground">Loading...</p>
        ) : (
          <TooltipProvider delayDuration={150}>
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-medium">Cost over time</h3>
                <div className="flex items-center gap-3 text-[11px]">
                  <span className="inline-flex items-center gap-1">
                    <span className="inline-block h-2 w-2 rounded-sm bg-[#3b82f6]" />
                    <span className="text-muted-foreground">Native</span>
                  </span>
                  <span className="inline-flex items-center gap-1">
                    <span className="inline-block h-2 w-2 rounded-sm bg-[#f59e0b]" />
                    <span className="text-muted-foreground">Estimated</span>
                  </span>
                </div>
              </div>
              <CostChart series={data.series} bucket={data.bucket} />
            </div>

            <div className="flex flex-wrap gap-2 text-xs">
              <Badge variant="outline">
                total {formatCostUsd(data.totals.cost_usd)}
              </Badge>
              <Badge variant="outline">
                native {formatCostUsd(data.totals.cost_native_usd)}
              </Badge>
              <Badge variant="outline">
                estimated {formatCostUsd(data.totals.cost_estimated_usd)}
              </Badge>
              <Badge variant="outline">
                {data.totals.trial_count.toLocaleString()} trials
              </Badge>
              <Badge variant="outline">
                {data.totals.experiment_count.toLocaleString()} experiments
              </Badge>
              <Badge variant="outline">
                {data.totals.user_count.toLocaleString()} users
              </Badge>
            </div>

            <section className="space-y-2">
              <h3 className="text-sm font-medium">Cost by user</h3>
              <UserTable users={data.by_user} />
            </section>

            <section className="space-y-2">
              <h3 className="text-sm font-medium">Cost by model</h3>
              <ModelTable models={data.by_model} />
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

function UserTable({ users }: { users: CostUserBreakdown[] }) {
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
          <TableHead className="text-right">Est</TableHead>
          <TableHead className="text-right">Trials</TableHead>
          <TableHead className="text-right">Exps</TableHead>
          <TableHead>Top models</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {users.map((user) => (
          <TableRow key={user.owner_user_id ?? "unattributed"}>
            <TableCell>
              <div className="flex flex-col">
                <span className="text-xs font-medium">{userLabel(user)}</span>
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
            <TableCell className="text-right font-mono text-xs font-medium">
              {formatCostUsd(user.cost_usd)}
            </TableCell>
            <TableCell className="text-right">
              <EstimatedBadge
                cost={user.cost_usd}
                estimated={user.cost_estimated_usd}
              />
            </TableCell>
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

function ModelTable({ models }: { models: CostModelBreakdown[] }) {
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
          <TableHead className="text-right">Est</TableHead>
          <TableHead className="text-right">Trials</TableHead>
          <TableHead className="text-right">Input</TableHead>
          <TableHead className="text-right">Output</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {models.map((model) => (
          <TableRow key={`${model.model}-${model.provider}`}>
            <TableCell>
              <ModelLabel model={model} />
            </TableCell>
            <TableCell className="text-right font-mono text-xs font-medium">
              {formatCostUsd(model.cost_usd)}
            </TableCell>
            <TableCell className="text-right">
              <EstimatedBadge
                cost={model.cost_usd}
                estimated={model.cost_estimated_usd}
              />
            </TableCell>
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
          <TableHead className="text-right">Cost</TableHead>
          <TableHead className="text-right">Est</TableHead>
          <TableHead className="text-right">Trials</TableHead>
          <TableHead>Models</TableHead>
          <TableHead className="text-right">Activity</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {experiments.map((exp) => (
          <TableRow key={exp.experiment_id}>
            <TableCell className="max-w-[220px]">
              <Link
                href={`/experiments/${encodeExperimentRouteParam(exp.experiment_id)}`}
                className="truncate text-xs font-medium text-[#5d77a5] hover:underline dark:text-[#a8b8d2]"
                title={exp.name ?? exp.experiment_id}
              >
                {exp.name ?? exp.experiment_id}
              </Link>
            </TableCell>
            <TableCell className="text-muted-foreground text-[11px]">
              {exp.owner_name ?? exp.owner_email ?? exp.owner_user_id ?? "—"}
            </TableCell>
            <TableCell className="text-right font-mono text-xs font-medium">
              {formatCostUsd(exp.cost_usd)}
            </TableCell>
            <TableCell className="text-right">
              <EstimatedBadge
                cost={exp.cost_usd}
                estimated={exp.cost_estimated_usd}
              />
            </TableCell>
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

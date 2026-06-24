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
import type {
  CostBreakdownResponse,
  CostExperimentBreakdown,
  CostModelBreakdown,
  CostUserBreakdown,
} from "@/lib/types";
import { fetcher } from "@/lib/api";
import { formatCostUsd } from "@/lib/format";
import { encodeExperimentRouteParam } from "@/lib/utils";
import { QueueKeyIcon } from "@/components/queue-key-icon";
import { AlertCircle, DollarSign, RefreshCw } from "lucide-react";

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
// Window summary cards (24h / 7d / 30d / all-time)
// =============================================================================

function WindowSummary({
  data,
  selectedWindowDays,
}: {
  data: CostBreakdownResponse;
  selectedWindowDays: number | null;
}) {
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      {data.windows.map((w) => {
        const active = w.window_days === selectedWindowDays;
        return (
          <div
            key={w.label}
            className={`rounded-lg border p-3 ${
              active ? "border-primary bg-primary/5" : "border-border"
            }`}
          >
            <div className="text-muted-foreground text-xs">{w.label}</div>
            <div className="mt-1 text-xl font-semibold">
              {formatCostUsd(w.cost_usd)}
            </div>
            <div className="text-muted-foreground mt-1 text-[11px]">
              {w.trial_count.toLocaleString()} trials ·{" "}
              {formatTokens(w.total_tokens)} tok
            </div>
          </div>
        );
      })}
    </div>
  );
}

// =============================================================================
// Top-level card
// =============================================================================

export function CostBreakdownCard() {
  const [windowDays, setWindowDays] = useState("7");
  const selectedWindowDays = windowDays === "0" ? null : Number(windowDays);

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
          Total trial spend across all organizations. Cost uses the
          runtime-reported value when present and a per-model token estimate
          otherwise. Per-user attribution follows experiment ownership.
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
            <WindowSummary
              data={data}
              selectedWindowDays={selectedWindowDays}
            />

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

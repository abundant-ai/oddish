"use client";

import { use, useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
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
  CostModelBreakdown,
  UserCostBreakdownResponse,
  UserCostTaskBreakdown,
} from "@/lib/types";
import { fetcher } from "@/lib/api";
import { formatCostUsd } from "@/lib/format";
import { CostChart } from "@/components/cost-breakdown-card";
import { QueueKeyIcon } from "@/components/queue-key-icon";
import { ArrowLeft, DollarSign, Info } from "lucide-react";

const WINDOW_OPTIONS: { value: string; label: string }[] = [
  { value: "1", label: "Last 24 hours" },
  { value: "7", label: "Last 7 days" },
  { value: "30", label: "Last 30 days" },
  { value: "90", label: "Last 90 days" },
  { value: "0", label: "All time" },
];

const TASK_LIMIT = 100;

function estimatedPct(cost: number, estimated: number): number {
  if (cost <= 0) return 0;
  return Math.round((estimated / cost) * 100);
}

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

function userLabel(data: UserCostBreakdownResponse): string {
  return data.name || data.email || data.github_username || data.billed_user_id;
}

function TaskTable({ tasks }: { tasks: UserCostTaskBreakdown[] }) {
  if (tasks.length === 0)
    return (
      <p className="text-muted-foreground py-3 text-xs">
        No trial spend in this window.
      </p>
    );
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Task</TableHead>
          <TableHead className="text-right">Cost</TableHead>
          <TableHead className="text-right">Trials</TableHead>
          <TableHead>Models</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {tasks.map((task) => (
          <TableRow key={task.task_id}>
            <TableCell className="max-w-[280px]">
              <Link
                href={`/tasks/${encodeURIComponent(task.task_id)}`}
                className="truncate text-xs font-medium text-[#5d77a5] hover:underline dark:text-[#a8b8d2]"
                title={task.task_name ?? task.task_id}
              >
                {task.task_name ?? task.task_id}
              </Link>
            </TableCell>
            <CostCell
              cost={task.cost_usd}
              estimated={task.cost_estimated_usd}
            />
            <TableCell className="text-right font-mono text-xs">
              {task.trial_count.toLocaleString()}
            </TableCell>
            <TableCell>
              <ModelMix models={task.models} />
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

export default function AdminUserCostPage({
  params,
}: {
  params: Promise<{ userId: string }>;
}) {
  const { userId } = use(params);
  const [windowDays, setWindowDays] = useState("7");

  const { data, error, isLoading } = useSWR<UserCostBreakdownResponse>(
    `/api/admin/users/${encodeURIComponent(userId)}/costs?window_days=${windowDays}&task_limit=${TASK_LIMIT}`,
    fetcher,
    { refreshInterval: 30000 },
  );

  const windowLabel =
    WINDOW_OPTIONS.find((o) => o.value === windowDays)?.label ?? windowDays;
  const status = (error as { status?: number } | undefined)?.status;

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <DollarSign className="h-5 w-5" />
            <CardTitle className="text-base">
              {data ? userLabel(data) : "User cost"}
            </CardTitle>
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
            <Link href="/admin">
              <Button variant="outline" size="sm" className="h-8 gap-1.5 text-xs">
                <ArrowLeft className="h-3.5 w-3.5" />
                Admin
              </Button>
            </Link>
          </div>
        </div>
        <div className="text-muted-foreground text-xs">
          {data?.email && <span>{data.email}</span>}
        </div>
        <p className="text-muted-foreground text-xs">
          Billed-user attribution — spend billed to this user. Totals can
          differ slightly from the Costs tab: this view counts settled trials
          by finish time (deleted included); the Costs tab buckets by creation
          time.
        </p>
      </CardHeader>
      <CardContent className="space-y-6">
        {error ? (
          <Alert variant="destructive">
            <AlertTitle>Failed to load user cost</AlertTitle>
            <AlertDescription>
              {status === 403
                ? "Check if you have admin access."
                : error instanceof Error
                  ? error.message
                  : "Check if you have admin access."}
            </AlertDescription>
          </Alert>
        ) : isLoading || !data ? (
          <div className="space-y-4">
            <div className="flex gap-2">
              <Skeleton className="h-6 w-24" />
              <Skeleton className="h-6 w-20" />
              <Skeleton className="h-6 w-20" />
            </div>
            <Skeleton className="h-[240px] w-full" />
            <Skeleton className="h-40 w-full" />
          </div>
        ) : (
          <TooltipProvider delayDuration={150}>
            <div className="flex flex-wrap gap-2 text-xs">
              <Badge variant="outline">
                total {formatCostUsd(data.totals.cost_usd)}
                <EstimateMarker
                  cost={data.totals.cost_usd}
                  estimated={data.totals.cost_estimated_usd}
                />
              </Badge>
              <Badge variant="outline">
                estimated {formatCostUsd(data.totals.cost_estimated_usd)}
              </Badge>
              <Badge variant="outline">
                {data.totals.trial_count.toLocaleString()} trials
              </Badge>
              <Badge variant="outline">
                {data.totals.task_count.toLocaleString()} tasks
              </Badge>
            </div>

            <div className="space-y-2">
              <h3 className="text-sm font-medium">Cost over time</h3>
              <CostChart series={data.series_by_model} bucket={data.bucket} />
            </div>

            <section className="space-y-2">
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-medium">Cost by task</h3>
                {data.totals.task_count > data.tasks.length && (
                  <span className="text-muted-foreground text-[11px]">
                    top {data.tasks.length} of{" "}
                    {data.totals.task_count.toLocaleString()}
                  </span>
                )}
              </div>
              <TaskTable tasks={data.tasks} />
            </section>
          </TooltipProvider>
        )}
      </CardContent>
    </Card>
  );
}

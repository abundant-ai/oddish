"use client";

import { useMemo, useState } from "react";
import useSWR from "swr";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { fetcher } from "@/lib/api";
import type {
  ExperimentCellAgent,
  ResolvedExperiment,
  ResolvedExperimentCell,
} from "@/lib/types";
import { encodeExperimentRouteParam } from "@/lib/utils";

type Props = {
  experimentId: string;
  canEdit: boolean;
};

type Pivot = {
  agents: { key: string; agent: ExperimentCellAgent }[];
  rows: {
    taskVersionId: string;
    taskId: string;
    taskName: string | null;
    version: number | null;
    cellsByAgent: Record<string, ResolvedExperimentCell | undefined>;
  }[];
};

function pivotCells(cells: ResolvedExperimentCell[]): Pivot {
  const agentByKey = new Map<string, ExperimentCellAgent>();
  const rowsByTV = new Map<string, Pivot["rows"][number]>();

  for (const cell of cells) {
    agentByKey.set(cell.agent.equivalence_key, cell.agent);
    const row = rowsByTV.get(cell.task_version_id) ?? {
      taskVersionId: cell.task_version_id,
      taskId: cell.task_id,
      taskName: cell.task_name,
      version: cell.task_version,
      cellsByAgent: {},
    };
    row.cellsByAgent[cell.agent.equivalence_key] = cell;
    rowsByTV.set(cell.task_version_id, row);
  }

  const agents = Array.from(agentByKey.entries())
    .sort((a, b) => {
      const an = `${a[1].harness}|${a[1].model ?? ""}`;
      const bn = `${b[1].harness}|${b[1].model ?? ""}`;
      return an.localeCompare(bn);
    })
    .map(([key, agent]) => ({ key, agent }));

  const rows = Array.from(rowsByTV.values()).sort((a, b) => {
    const an = a.taskName ?? a.taskId;
    const bn = b.taskName ?? b.taskId;
    return an.localeCompare(bn) || (a.version ?? 0) - (b.version ?? 0);
  });

  return { agents, rows };
}

function formatAgentLabel(agent: ExperimentCellAgent): string {
  const model = agent.model ? ` · ${agent.model}` : "";
  return `${agent.harness}${model}`;
}

function formatReward(reward: number | null | undefined): string {
  if (reward === null || reward === undefined) return "—";
  return reward.toFixed(2);
}

function CellBadge({ cell }: { cell: ResolvedExperimentCell | undefined }) {
  if (!cell) {
    return <span className="text-muted-foreground text-xs">—</span>;
  }
  const ratioColor =
    cell.gap === 0
      ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300"
      : cell.have_n_running > 0
        ? "bg-amber-500/15 text-amber-700 dark:text-amber-300"
        : "bg-rose-500/15 text-rose-700 dark:text-rose-300";

  return (
    <TooltipProvider delayDuration={150}>
      <Tooltip>
        <TooltipTrigger asChild>
          <div
            className={`inline-flex flex-col items-start gap-0.5 rounded-md px-2 py-1 ${ratioColor}`}
          >
            <span className="font-mono text-xs">
              {cell.have_n_successful}/{cell.target_n_trials}
            </span>
            <span className="text-[10px] opacity-80">
              μ {formatReward(cell.mean_reward)}
            </span>
          </div>
        </TooltipTrigger>
        <TooltipContent side="top" className="max-w-xs space-y-1 text-xs">
          <div className="font-mono">
            successful: {cell.have_n_successful} · failed: {cell.have_n_failed} ·
            running: {cell.have_n_running}
          </div>
          <div>
            target: {cell.target_n_trials} · gap:{" "}
            <span className="font-semibold">{cell.gap}</span>
          </div>
          {cell.last_run_at ? (
            <div className="text-muted-foreground">
              last run {new Date(cell.last_run_at).toLocaleString()}
            </div>
          ) : null}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export function ExperimentCellMatrix({ experimentId, canEdit }: Props) {
  const encodedId = encodeExperimentRouteParam(experimentId);
  const url = experimentId
    ? `/api/experiments/${encodedId}/resolved`
    : null;

  const {
    data,
    error,
    isLoading,
    mutate,
  } = useSWR<ResolvedExperiment>(url, fetcher, {
    refreshInterval: 30_000,
    revalidateOnFocus: false,
  });

  const pivot = useMemo<Pivot | null>(
    () => (data ? pivotCells(data.cells) : null),
    [data],
  );

  const [isBackfilling, setIsBackfilling] = useState(false);
  const [backfillError, setBackfillError] = useState<string | null>(null);

  const onBackfill = async () => {
    setBackfillError(null);
    setIsBackfilling(true);
    try {
      const res = await fetch(
        `/api/experiments/${encodedId}/backfill`,
        { method: "POST", credentials: "include" },
      );
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || `Backfill failed (${res.status})`);
      }
      await mutate();
    } catch (err) {
      setBackfillError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsBackfilling(false);
    }
  };

  if (isLoading) {
    return (
      <div className="space-y-2">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-32 w-full" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm">
        Failed to load cell matrix: {String((error as Error).message)}
      </div>
    );
  }

  if (!data || !pivot || pivot.rows.length === 0) {
    return (
      <div className="rounded-md border border-dashed p-6 text-center text-sm text-muted-foreground">
        No cells yet. Add task versions and agents to start collecting evidence.
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-3 text-sm">
          <span className="font-medium">{data.experiment_name}</span>
          <Badge variant={data.total_gap === 0 ? "secondary" : "outline"}>
            gap: {data.total_gap}
          </Badge>
          <span className="text-muted-foreground">
            {pivot.rows.length} task version{pivot.rows.length === 1 ? "" : "s"} ·{" "}
            {pivot.agents.length} agent{pivot.agents.length === 1 ? "" : "s"}
          </span>
        </div>
        {canEdit ? (
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              onClick={onBackfill}
              disabled={isBackfilling || data.total_gap === 0}
              variant={data.total_gap > 0 ? "default" : "outline"}
            >
              {isBackfilling
                ? "Enqueuing…"
                : data.total_gap === 0
                  ? "No gaps"
                  : `Backfill ${data.total_gap}`}
            </Button>
          </div>
        ) : null}
      </div>

      {backfillError ? (
        <div className="rounded-md border border-destructive/40 bg-destructive/5 p-2 text-xs">
          {backfillError}
        </div>
      ) : null}

      <div className="overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="sticky left-0 z-10 bg-background">
                Task version
              </TableHead>
              {pivot.agents.map(({ key, agent }) => (
                <TableHead
                  key={key}
                  className="whitespace-nowrap text-center font-mono text-xs"
                >
                  {formatAgentLabel(agent)}
                  <div className="text-[10px] font-normal text-muted-foreground">
                    {agent.provider}
                  </div>
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {pivot.rows.map((row) => (
              <TableRow key={row.taskVersionId}>
                <TableCell className="sticky left-0 z-10 bg-background">
                  <div className="flex flex-col">
                    <span className="font-medium">
                      {row.taskName ?? row.taskId}
                    </span>
                    <span className="text-xs text-muted-foreground">
                      v{row.version ?? "?"}
                    </span>
                  </div>
                </TableCell>
                {pivot.agents.map(({ key }) => (
                  <TableCell key={key} className="text-center">
                    <CellBadge cell={row.cellsByAgent[key]} />
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}

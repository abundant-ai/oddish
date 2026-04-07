"use client";

import { useEffect, useMemo, useState } from "react";
import useSWR from "swr";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { fetcher } from "@/lib/api";
import { STATUS_CONFIG, getMatrixStatus } from "@/lib/status-config";
import type { TaskBrowseItem, TaskBrowseResponse } from "@/lib/types";
import { formatRelativeTime, formatShortDateTime } from "@/lib/utils";
import { ChevronLeft, ChevronRight, Loader2 } from "lucide-react";

const PAGE_SIZE = 25;

function useDebouncedValue<T>(value: T, delayMs: number) {
  const [debouncedValue, setDebouncedValue] = useState(value);

  useEffect(() => {
    const timeoutId = window.setTimeout(
      () => setDebouncedValue(value),
      delayMs
    );
    return () => window.clearTimeout(timeoutId);
  }, [delayMs, value]);

  return debouncedValue;
}

function TaskCardsSkeleton() {
  return (
    <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
      {Array.from({ length: 6 }).map((_, index) => (
        <div
          key={index}
          className="rounded-lg border border-[#6f88b4]/20 bg-card/95 p-4 shadow-sm"
        >
          <div className="space-y-3">
            <div className="flex items-start justify-between gap-3">
              <div className="space-y-2">
                <Skeleton className="h-5 w-36" />
                <Skeleton className="h-5 w-12" />
              </div>
              <Skeleton className="h-4 w-20" />
            </div>
            <Skeleton className="h-16 w-full" />
            <div className="grid grid-cols-3 gap-3">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
            </div>
            <Skeleton className="h-4 w-40" />
          </div>
        </div>
      ))}
    </div>
  );
}

function ExperimentsCell({ task }: { task: TaskBrowseItem }) {
  if (task.experiments.length === 0) {
    return <span className="text-muted-foreground">—</span>;
  }

  const names = task.experiments.map((experiment) => experiment.name);
  const visibleNames = names.slice(0, 3);
  const remaining = names.length - visibleNames.length;

  return (
    <div className="max-w-[280px] text-xs text-muted-foreground">
      <span>{visibleNames.join(", ")}</span>
      {remaining > 0 ? (
        <>
          <span>, </span>
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                type="button"
                className="font-medium text-[#5d77a5] transition-colors hover:text-[#526a95] dark:text-[#a8b8d2] dark:hover:text-[#c0cde1]"
              >
                +{remaining} more
              </button>
            </TooltipTrigger>
            <TooltipContent className="max-w-sm">
              {names.join(", ")}
            </TooltipContent>
          </Tooltip>
        </>
      ) : null}
    </div>
  );
}

function PassRateCell({ task }: { task: TaskBrowseItem }) {
  if (task.reward_total === 0) {
    return <span className="text-muted-foreground">—</span>;
  }

  const passRate = Math.round((task.reward_success / task.reward_total) * 100);
  const toneClass =
    passRate >= 80
      ? "text-[#5c8e43] dark:text-[#85b85c]"
      : passRate >= 50
        ? "text-yellow-400"
        : "text-rose-400";

  return (
    <div className="space-y-1">
      <div className={`font-medium ${toneClass}`}>{passRate}%</div>
      <div className="text-[11px] text-muted-foreground">
        {task.reward_success}/{task.reward_total}
      </div>
    </div>
  );
}

function TrialGraphics({ task }: { task: TaskBrowseItem }) {
  if (task.latest_trials.length === 0) {
    return (
      <div className="rounded-md border border-dashed border-border/70 px-3 py-4 text-center text-xs text-muted-foreground">
        No latest-version trials yet.
      </div>
    );
  }

  const statusCounts = task.latest_trials.reduce(
    (counts, trial) => {
      const status = getMatrixStatus(
        trial.status,
        trial.reward,
        trial.error_message
      );
      counts[status] += 1;
      return counts;
    },
    {
      pass: 0,
      fail: 0,
      "harness-error": 0,
      pending: 0,
      queued: 0,
      running: 0,
    } as Record<ReturnType<typeof getMatrixStatus>, number>
  );

  const summaryItems = [
    { key: "pass", label: "Pass", count: statusCounts.pass },
    { key: "fail", label: "Fail", count: statusCounts.fail },
    {
      key: "harness-error",
      label: "Harness",
      count: statusCounts["harness-error"],
    },
    {
      key: "pending",
      label: "Pending",
      count: statusCounts.pending + statusCounts.queued + statusCounts.running,
    },
  ] as const;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-1.5">
        {task.latest_trials.map((trial) => {
          const status = getMatrixStatus(
            trial.status,
            trial.reward,
            trial.error_message
          );
          const config = STATUS_CONFIG[status];

          return (
            <Tooltip key={trial.id}>
              <TooltipTrigger asChild>
                <div
                  className={`h-5 w-5 rounded-[4px] border ${config.matrixClass}`}
                  aria-label={`${trial.name} ${config.shortLabel}`}
                />
              </TooltipTrigger>
              <TooltipContent>
                <div className="space-y-0.5">
                  <div className="font-medium">{trial.name}</div>
                  <div className="text-muted-foreground">
                    {config.shortLabel}
                  </div>
                </div>
              </TooltipContent>
            </Tooltip>
          );
        })}
      </div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {summaryItems.map((item) => {
          const config = STATUS_CONFIG[item.key];
          return (
            <div
              key={item.key}
              className="rounded-md border border-border/60 bg-muted/30 px-2.5 py-2"
            >
              <div className="flex items-center gap-2">
                <span
                  className={`inline-flex h-2.5 w-2.5 rounded-full ${config.bracketClass}`}
                />
                <span className="text-[11px] text-muted-foreground">
                  {item.label}
                </span>
              </div>
              <div className="mt-1 font-mono text-sm font-semibold">
                {item.count}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function TaskCard({ task }: { task: TaskBrowseItem }) {
  return (
    <Card className="border-[#6f88b4]/20 bg-card/95 shadow-sm">
      <CardHeader className="space-y-3 pb-3">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0 space-y-2">
            <div className="font-mono text-sm font-semibold text-foreground">
              {task.name}
            </div>
            <Badge variant="outline" className="w-fit font-mono text-[11px]">
              v{task.current_version ?? "—"}
            </Badge>
          </div>
          <div className="shrink-0 text-right">
            <div className="text-[11px] uppercase tracking-wide text-muted-foreground">
              Last run
            </div>
            <div className="mt-1 text-xs">
              {task.last_run_at ? formatRelativeTime(task.last_run_at) : "—"}
            </div>
            {task.last_run_at ? (
              <div className="text-[11px] text-muted-foreground">
                {formatShortDateTime(task.last_run_at)}
              </div>
            ) : null}
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-2">
          <div className="text-[11px] uppercase tracking-wide text-muted-foreground">
            Latest trials
          </div>
          <TrialGraphics task={task} />
        </div>
        <div className="grid gap-3 sm:grid-cols-3">
          <div className="rounded-md border border-border/60 bg-muted/30 px-3 py-2.5">
            <div className="text-[11px] uppercase tracking-wide text-muted-foreground">
              Trials
            </div>
            <div className="mt-1 font-mono text-sm font-semibold">
              {task.completed_trials}/{task.total_trials}
            </div>
          </div>
          <div className="rounded-md border border-border/60 bg-muted/30 px-3 py-2.5">
            <div className="text-[11px] uppercase tracking-wide text-muted-foreground">
              Pass rate
            </div>
            <div className="mt-1 text-sm font-semibold">
              <PassRateCell task={task} />
            </div>
          </div>
          <div className="rounded-md border border-border/60 bg-muted/30 px-3 py-2.5">
            <div className="text-[11px] uppercase tracking-wide text-muted-foreground">
              Experiments
            </div>
            <div className="mt-1">
              <ExperimentsCell task={task} />
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

export function TasksPageClient({
  initialData,
}: {
  initialData?: TaskBrowseResponse | null;
}) {
  const [searchQuery, setSearchQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const debouncedQuery = useDebouncedValue(searchQuery.trim(), 300);

  useEffect(() => {
    setOffset(0);
  }, [debouncedQuery]);

  const swrKey = useMemo(() => {
    const params = new URLSearchParams({
      limit: String(PAGE_SIZE),
      offset: String(offset),
    });
    if (debouncedQuery) {
      params.set("query", debouncedQuery);
    }
    return `/api/tasks/browse?${params.toString()}`;
  }, [debouncedQuery, offset]);

  const { data, error, isLoading, isValidating } = useSWR<TaskBrowseResponse>(
    swrKey,
    fetcher,
    {
      refreshInterval: 60000,
      revalidateOnFocus: false,
      keepPreviousData: true,
      fallbackData:
        offset === 0 && debouncedQuery.length === 0
          ? (initialData ?? undefined)
          : undefined,
    }
  );

  const items = data?.items ?? [];
  const hasMore = data?.has_more ?? false;
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;
  const isRefreshing = !error && !isLoading && isValidating;

  return (
    <TooltipProvider>
      <div className="space-y-6">
        <h1 className="text-2xl font-bold">Tasks</h1>

        <Card className="border-[#6f88b4]/20 shadow-sm">
          <CardHeader className="flex flex-col gap-3 pb-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="space-y-1">
              <CardTitle className="text-base">Task Browser</CardTitle>
              <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
                <span>
                  Showing {items.length}
                  {" • "}Page {currentPage}
                </span>
                {isRefreshing ? (
                  <span className="inline-flex items-center gap-1">
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    Refreshing
                  </span>
                ) : null}
              </div>
            </div>
            <Input
              value={searchQuery}
              onChange={(event) => setSearchQuery(event.target.value)}
              placeholder="Search tasks"
              className="h-8 w-full border-[#6f88b4]/20 sm:w-[260px]"
            />
          </CardHeader>
          <CardContent className="space-y-4">
            {error ? (
              <Alert variant="destructive">
                <AlertTitle>Failed to load tasks</AlertTitle>
                <AlertDescription>
                  Check the API connection and try again.
                </AlertDescription>
              </Alert>
            ) : isLoading && items.length === 0 ? (
              <TaskCardsSkeleton />
            ) : items.length === 0 ? (
              <div className="rounded-lg border border-dashed border-[#6f88b4]/30 bg-card/60 px-6 py-10 text-center text-sm text-muted-foreground">
                {debouncedQuery
                  ? "No tasks match the current search."
                  : "No tasks have been created yet."}
              </div>
            ) : (
              <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                {items.map((task) => (
                  <TaskCard key={task.id} task={task} />
                ))}
              </div>
            )}

            <div className="flex items-center justify-between gap-2">
              <div className="text-xs text-muted-foreground">
                {items.length > 0
                  ? `${offset + 1}-${offset + items.length}`
                  : "0"}{" "}
                shown
              </div>
              <div className="flex items-center gap-2">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="h-8 px-3 text-[11px]"
                  onClick={() =>
                    setOffset((currentOffset) =>
                      Math.max(currentOffset - PAGE_SIZE, 0)
                    )
                  }
                  disabled={offset === 0 || isValidating}
                >
                  <ChevronLeft className="mr-1 h-3.5 w-3.5" />
                  Previous page
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="h-8 px-3 text-[11px]"
                  onClick={() =>
                    setOffset((currentOffset) => currentOffset + PAGE_SIZE)
                  }
                  disabled={!hasMore || isValidating}
                >
                  Next page
                  <ChevronRight className="ml-1 h-3.5 w-3.5" />
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>
      </div>
    </TooltipProvider>
  );
}

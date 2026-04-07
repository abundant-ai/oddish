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
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { fetcher } from "@/lib/api";
import type { TaskBrowseItem, TaskBrowseResponse } from "@/lib/types";
import { formatRelativeTime, formatShortDateTime } from "@/lib/utils";
import { ChevronLeft, ChevronRight, Loader2 } from "lucide-react";

const PAGE_SIZE = 25;

function useDebouncedValue<T>(value: T, delayMs: number) {
  const [debouncedValue, setDebouncedValue] = useState(value);

  useEffect(() => {
    const timeoutId = window.setTimeout(
      () => setDebouncedValue(value),
      delayMs,
    );
    return () => window.clearTimeout(timeoutId);
  }, [delayMs, value]);

  return debouncedValue;
}

function TaskTableSkeleton() {
  return (
    <div className="overflow-hidden rounded-lg border border-border/60">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Task</TableHead>
            <TableHead>Version</TableHead>
            <TableHead>Trials</TableHead>
            <TableHead>Pass rate</TableHead>
            <TableHead>Experiments</TableHead>
            <TableHead className="text-right">Last run</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody className="[&_td]:py-4">
          {Array.from({ length: 6 }).map((_, index) => (
            <TableRow key={index}>
              <TableCell>
                <Skeleton className="h-4 w-40" />
              </TableCell>
              <TableCell>
                <Skeleton className="h-5 w-24" />
              </TableCell>
              <TableCell>
                <div className="space-y-2">
                  <Skeleton className="h-4 w-20" />
                  <Skeleton className="h-2 w-28" />
                </div>
              </TableCell>
              <TableCell>
                <Skeleton className="h-4 w-20" />
              </TableCell>
              <TableCell>
                <Skeleton className="h-4 w-36" />
              </TableCell>
              <TableCell className="text-right">
                <Skeleton className="ml-auto h-4 w-20" />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function formatVersionCount(versionCount: number) {
  return `${versionCount} version${versionCount === 1 ? "" : "s"}`;
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

function TrialsCell({ task }: { task: TaskBrowseItem }) {
  if (task.total_trials === 0) {
    return <span className="text-muted-foreground">—</span>;
  }

  const completionRatio =
    task.total_trials > 0 ? task.completed_trials / task.total_trials : 0;

  return (
    <div className="min-w-[132px] space-y-1.5">
      <div className="flex items-center justify-between gap-2">
        <span className="font-medium">
          {task.completed_trials}/{task.total_trials}
        </span>
        {task.failed_trials > 0 ? (
          <span className="text-[11px] text-muted-foreground">
            {task.failed_trials} failed
          </span>
        ) : null}
      </div>
      <div className="h-2 overflow-hidden rounded-full bg-muted">
        <div
          className="h-full rounded-full bg-[#5c8e43] dark:bg-[#85b85c]"
          style={{
            width: `${Math.max(completionRatio * 100, completionRatio > 0 ? 4 : 0)}%`,
          }}
        />
      </div>
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
    },
  );

  const items = data?.items ?? [];
  const hasMore = data?.has_more ?? false;
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;
  const isRefreshing = !error && !isLoading && isValidating;

  return (
    <TooltipProvider>
      <div className="space-y-6">
        <div className="space-y-1">
          <h1 className="text-2xl font-bold">Tasks</h1>
          <p className="text-sm text-muted-foreground">
            Browse the latest version of every task across experiments.
          </p>
        </div>

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
              <TaskTableSkeleton />
            ) : items.length === 0 ? (
              <div className="rounded-lg border border-dashed border-[#6f88b4]/30 bg-card/60 px-6 py-10 text-center text-sm text-muted-foreground">
                {debouncedQuery
                  ? "No tasks match the current search."
                  : "No tasks have been created yet."}
              </div>
            ) : (
              <div className="overflow-hidden rounded-lg border border-border/60">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Task</TableHead>
                      <TableHead>Version</TableHead>
                      <TableHead>Trials</TableHead>
                      <TableHead>Pass rate</TableHead>
                      <TableHead>Experiments</TableHead>
                      <TableHead className="text-right">Last run</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody className="[&_td]:align-top [&_td]:text-xs">
                    {items.map((task) => (
                      <TableRow key={task.id}>
                        <TableCell className="w-[28%]">
                          <span className="cursor-pointer font-mono text-sm font-semibold text-foreground transition-colors hover:text-[#a8b8d2]">
                            {task.name}
                          </span>
                        </TableCell>
                        <TableCell>
                          <div className="flex flex-col gap-1">
                            <Badge
                              variant="outline"
                              className="w-fit font-mono text-[11px]"
                            >
                              v{task.current_version ?? "—"}
                            </Badge>
                            <span className="text-[11px] text-muted-foreground">
                              ({formatVersionCount(task.version_count)})
                            </span>
                          </div>
                        </TableCell>
                        <TableCell>
                          <TrialsCell task={task} />
                        </TableCell>
                        <TableCell>
                          <PassRateCell task={task} />
                        </TableCell>
                        <TableCell>
                          <ExperimentsCell task={task} />
                        </TableCell>
                        <TableCell className="whitespace-nowrap text-right">
                          {task.last_run_at ? (
                            <div className="space-y-1">
                              <div>{formatRelativeTime(task.last_run_at)}</div>
                              <div className="text-[11px] text-muted-foreground">
                                {formatShortDateTime(task.last_run_at)}
                              </div>
                            </div>
                          ) : (
                            <span className="text-muted-foreground">—</span>
                          )}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}

            <div className="flex items-center justify-between gap-2">
              <div className="text-xs text-muted-foreground">
                {items.length > 0 ? `${offset + 1}-${offset + items.length}` : "0"} shown
              </div>
              <div className="flex items-center gap-2">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="h-8 px-3 text-[11px]"
                  onClick={() =>
                    setOffset((currentOffset) =>
                      Math.max(currentOffset - PAGE_SIZE, 0),
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

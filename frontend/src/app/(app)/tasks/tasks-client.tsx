"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import useSWR from "swr";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge, badgeVariants } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { ChatButton } from "@/components/cc-chat/chat-button";
import { ImportDialog } from "@/components/import-dialog";
import { SavedFiltersMenu } from "@/components/saved-filters-menu";
import { TagChip } from "@/components/tag-chip";
import { fetcher } from "@/lib/api";
import { formatCostUsd } from "@/lib/format";
import { parseTaskSearch } from "@/lib/tag-query";
import {
  formatPartialRewardBadgeValue,
  formatRewardPercent,
  formatRewardValue,
  getMatrixStatus,
  getRewardStyle,
  STATUS_CONFIG,
} from "@/lib/status-config";
import type { TaskBrowseItem, TaskBrowseResponse } from "@/lib/types";
import {
  cn,
  encodeExperimentRouteParam,
  formatRelativeTime,
  formatShortDateTime,
  prBadge,
  taskPrUrl,
} from "@/lib/utils";
import {
  ChevronLeft,
  ChevronRight,
  CircleHelp,
  ExternalLink,
  GitPullRequest,
  Loader2,
} from "lucide-react";

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

function SearchSyntaxRow({ example, hint }: { example: string; hint: string }) {
  return (
    <p className="flex items-baseline gap-2">
      <code className="bg-muted shrink-0 rounded px-1 font-mono">
        {example}
      </code>
      <span className="text-muted-foreground">{hint}</span>
    </p>
  );
}

function TaskCardsSkeleton() {
  return (
    <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
      {Array.from({ length: 6 }).map((_, index) => (
        <div
          key={index}
          className="bg-card/95 rounded-lg border border-[#6f88b4]/20 p-4 shadow-xs"
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

  return (
    <div className="text-muted-foreground flex flex-wrap gap-x-2 gap-y-1 text-xs">
      {task.experiments.map((experiment, index) => (
        <span key={experiment.id}>
          <Link
            href={`/experiments/${encodeExperimentRouteParam(experiment.id)}`}
            className="text-[#5d77a5] transition-colors hover:text-[#526a95] dark:text-[#a8b8d2] dark:hover:text-[#c0cde1]"
          >
            {experiment.name}
          </Link>
          {index < task.experiments.length - 1 ? "," : null}
        </span>
      ))}
    </div>
  );
}

function getLatestTrialStatusCounts(task: TaskBrowseItem) {
  return task.latest_trials.reduce(
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
      partial: 0,
      fail: 0,
      "harness-error": 0,
      pending: 0,
      queued: 0,
      running: 0,
    } as Record<ReturnType<typeof getMatrixStatus>, number>
  );
}

function PassRateCell({ task }: { task: TaskBrowseItem }) {
  const rewardSum = task.reward_sum ?? task.reward_success;
  const hasScore = task.reward_total > 0;
  const avgScore = hasScore
    ? Math.round((rewardSum / task.reward_total) * 100)
    : null;
  const toneClass =
    avgScore == null
      ? "text-muted-foreground"
      : avgScore >= 80
        ? "text-[#5c8e43] dark:text-[#85b85c]"
        : avgScore >= 35
          ? "text-yellow-400"
          : "text-rose-400";
  const statusCounts = getLatestTrialStatusCounts(task);
  const summaryItems = [
    { key: "pass", label: "Pass", count: statusCounts.pass },
    { key: "partial", label: "Partial", count: statusCounts.partial },
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
    <div className="space-y-1.5">
      <div className="flex items-baseline justify-between gap-3">
        <div className={`text-base leading-none font-medium ${toneClass}`}>
          {avgScore == null ? "—" : `${avgScore}%`}
        </div>
        <div className="text-muted-foreground text-[11px] leading-none">
          {hasScore
            ? `${rewardSum.toFixed(2)}/${task.reward_total}`
            : "No completed trials"}
        </div>
      </div>
      {task.latest_trials.length > 0 ? (
        <div className="text-muted-foreground flex flex-wrap gap-x-2.5 gap-y-0.5 text-[10px] leading-none">
          {summaryItems.map((item) => {
            const config = STATUS_CONFIG[item.key];
            return (
              <div
                key={item.key}
                className="flex items-center gap-1 whitespace-nowrap"
              >
                <span
                  className={`inline-flex h-2 w-2 rounded-full ${config.bracketClass}`}
                />
                <span>{item.label}</span>
                <span className="text-foreground font-mono">{item.count}</span>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="text-muted-foreground text-[10px] leading-none">
          No latest-version trials
        </div>
      )}
    </div>
  );
}

function TrialGraphics({ task }: { task: TaskBrowseItem }) {
  if (task.latest_trials.length === 0) {
    return (
      <div className="border-border/70 text-muted-foreground rounded-md border border-dashed px-3 py-3 text-center text-xs">
        No latest-version trials yet.
      </div>
    );
  }

  return (
    <div className="flex flex-wrap gap-1">
      {task.latest_trials.map((trial) => {
        const status = getMatrixStatus(
          trial.status,
          trial.reward,
          trial.error_message
        );
        const config = STATUS_CONFIG[status];
        const badgeLabel =
          status === "partial"
            ? formatPartialRewardBadgeValue(trial.reward)
            : null;

        return (
          <Tooltip key={trial.id}>
            <TooltipTrigger asChild>
              <div
                className={`flex h-[18px] w-[18px] items-center justify-center rounded-[4px] border font-mono leading-none font-semibold ${config.matrixClass} ${status === "partial" ? "text-[7px] tracking-[-0.03em]" : ""}`}
                style={getRewardStyle(trial.reward)}
                aria-label={`${trial.name} ${config.shortLabel}`}
              >
                {badgeLabel}
              </div>
            </TooltipTrigger>
            <TooltipContent>
              <div className="space-y-0.5">
                <div className="font-medium">{trial.name}</div>
                <div className="text-muted-foreground">{config.shortLabel}</div>
                {trial.reward !== null && (
                  <div className="text-muted-foreground">
                    Score {formatRewardValue(trial.reward)} (
                    {formatRewardPercent(trial.reward)})
                  </div>
                )}
              </div>
            </TooltipContent>
          </Tooltip>
        );
      })}
    </div>
  );
}

function TaskCard({
  task,
  selected,
  onToggleSelected,
}: {
  task: TaskBrowseItem;
  selected: boolean;
  onToggleSelected: (task: TaskBrowseItem) => void;
}) {
  return (
    <Card
      className={cn(
        "bg-card/95 border-[#6f88b4]/20 shadow-xs transition-colors hover:border-[#6f88b4]/40",
        selected && "border-[#6f88b4]/70 ring-1 ring-[#6f88b4]/40"
      )}
    >
      <CardHeader className="space-y-2 px-5 pt-5 pb-2">
        <div className="flex items-start justify-between gap-3">
          <Checkbox
            checked={selected}
            onCheckedChange={() => onToggleSelected(task)}
            aria-label={`Select ${task.name} for cost total`}
            className="mt-0.5 shrink-0"
          />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <Link
                href={`/tasks/${encodeURIComponent(task.id)}`}
                className="text-foreground font-mono text-sm font-semibold transition-colors hover:text-[#5d77a5] dark:hover:text-[#a8b8d2]"
              >
                {task.name}
              </Link>
              <Badge variant="outline" className="w-fit font-mono text-[11px]">
                v{task.current_version ?? "—"}
              </Badge>
              {(() => {
                const meta = task.github_meta;
                const prUrl = taskPrUrl(task.link, meta);
                if (!prUrl) return null;
                const { label, number } = prBadge(prUrl, meta?.pr_number);
                const title = meta?.pr_title;
                return (
                  <a
                    href={prUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    title={
                      title
                        ? `${title} — view on GitHub`
                        : "View pull request on GitHub"
                    }
                    onClick={(e) => e.stopPropagation()}
                    className={cn(
                      badgeVariants({ variant: "outline" }),
                      "hover:bg-accent w-fit gap-1.5 font-mono text-[11px] transition-colors"
                    )}
                  >
                    <GitPullRequest className="h-3 w-3 shrink-0" aria-hidden />
                    <span className="max-w-[140px] min-w-0 truncate">
                      {label}
                      {number && (
                        <span className="text-muted-foreground">
                          {" "}
                          #{number}
                        </span>
                      )}
                    </span>
                    <ExternalLink
                      className="h-3 w-3 shrink-0 opacity-50"
                      aria-hidden
                    />
                  </a>
                );
              })()}
            </div>
          </div>
          <div className="flex shrink-0 flex-col items-end gap-2">
            <Link
              href={`/tasks/${task.id}/probe`}
              target="_blank"
              rel="noopener noreferrer"
              className="text-muted-foreground hover:text-foreground text-[11px] font-medium underline-offset-2 hover:underline"
            >
              Probe run →
            </Link>
            <div className="text-right">
              <div className="text-muted-foreground text-[11px] tracking-wide uppercase">
                Last run
              </div>
              <div className="mt-1 text-xs">
                {task.last_run_at ? formatRelativeTime(task.last_run_at) : "—"}
              </div>
              {task.last_run_at ? (
                <div className="text-muted-foreground text-[11px]">
                  {formatShortDateTime(task.last_run_at)}
                </div>
              ) : null}
            </div>
            <div className="text-right">
              <div className="text-muted-foreground text-[11px] tracking-wide uppercase">
                Cost
              </div>
              <div className="mt-1 text-sm font-semibold tabular-nums">
                {task.cost_trial_count > 0
                  ? `${task.cost_has_estimated && !task.cost_has_native ? "~" : ""}${formatCostUsd(task.cost_usd)}`
                  : "—"}
              </div>
              {task.cost_trial_count > 0 ? (
                <div className="text-muted-foreground text-[11px]">
                  {task.cost_trial_count} of {task.total_trials} priced
                </div>
              ) : null}
            </div>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-3 px-5 pb-5">
        {(task.user_tags ?? []).length > 0 ? (
          <div className="flex flex-wrap gap-1">
            {(task.user_tags ?? []).map((t) => (
              <TagChip key={t.tag_id} tag={t} />
            ))}
          </div>
        ) : null}
        <div className="space-y-1.5">
          <div className="text-muted-foreground text-[11px] tracking-wide uppercase">
            Latest trials
          </div>
          <TrialGraphics task={task} />
        </div>
        <div className="grid gap-2.5 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.45fr)]">
          <div className="border-border/60 bg-muted/30 rounded-md border px-3 py-2">
            <div className="text-muted-foreground text-[11px] tracking-wide uppercase">
              Avg score
            </div>
            <div className="mt-1 text-sm font-semibold">
              <PassRateCell task={task} />
            </div>
          </div>
          <div className="border-border/60 bg-muted/30 rounded-md border px-3 py-2">
            <div className="text-muted-foreground text-[11px] tracking-wide uppercase">
              Experiments
            </div>
            <div className="mt-0.5">
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
  initialQuery = "",
  orgId = null,
}: {
  initialData?: TaskBrowseResponse | null;
  initialQuery?: string;
  orgId?: string | null;
}) {
  const [searchQuery, setSearchQuery] = useState(initialQuery);
  const [offset, setOffset] = useState(0);
  // Multi-select cost tracking. Keyed by task id, stores enough to total
  // across pages (the browse page only holds the current 25 items).
  const [selectedCosts, setSelectedCosts] = useState<
    Map<string, { name: string; cost: number; estimated: boolean }>
  >(new Map());

  const toggleSelected = (task: TaskBrowseItem) => {
    setSelectedCosts((prev) => {
      const next = new Map(prev);
      if (next.has(task.id)) {
        next.delete(task.id);
      } else {
        next.set(task.id, {
          name: task.name,
          cost: task.cost_usd,
          estimated: task.cost_has_estimated && !task.cost_has_native,
        });
      }
      return next;
    });
  };
  const debouncedQuery = useDebouncedValue(searchQuery.trim(), 300);
  const parsed = useMemo(
    () => parseTaskSearch(debouncedQuery),
    [debouncedQuery]
  );

  useEffect(() => {
    setOffset(0);
  }, [debouncedQuery]);

  const swrKey = useMemo(() => {
    const params = new URLSearchParams({
      limit: String(PAGE_SIZE),
      offset: String(offset),
    });
    if (parsed.text) {
      params.set("query", parsed.text);
    }
    if (parsed.all.length) {
      params.set("tags", parsed.all.join(","));
    }
    if (parsed.any.length) {
      params.set("tags_any", parsed.any.join(","));
    }
    if (parsed.none.length) {
      params.set("tags_none", parsed.none.join(","));
    }
    return `/api/tasks/browse?${params.toString()}`;
  }, [offset, parsed]);

  const { data, error, isLoading, isValidating, mutate } =
    useSWR<TaskBrowseResponse>(swrKey, fetcher, {
      refreshInterval: 60000,
      revalidateOnFocus: false,
      keepPreviousData: true,
      fallbackData:
        offset === 0 && debouncedQuery.length === 0
          ? (initialData ?? undefined)
          : undefined,
    });

  const items = data?.items ?? [];
  const selectedTotal = useMemo(() => {
    let cost = 0;
    let anyEstimated = false;
    for (const entry of selectedCosts.values()) {
      cost += entry.cost;
      if (entry.estimated) anyEstimated = true;
    }
    return { count: selectedCosts.size, cost, anyEstimated };
  }, [selectedCosts]);
  const hasMore = data?.has_more ?? false;
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;
  const isRefreshing = !error && !isLoading && isValidating;

  return (
    <TooltipProvider>
      <div className="space-y-6">
        <Card className="border-[#6f88b4]/20 shadow-xs">
          <CardHeader className="flex flex-col gap-3 pb-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="space-y-1">
              <CardTitle className="text-base">Recent Tasks</CardTitle>
              <div className="text-muted-foreground flex items-center gap-2 text-[11px]">
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
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
              {orgId ? <ChatButton scopeKind="global" scopeId={orgId} /> : null}
              <div className="relative w-full sm:w-[260px]">
                <Input
                  value={searchQuery}
                  onChange={(event) => setSearchQuery(event.target.value)}
                  placeholder={'Search · word "phrase" -not tag:x'}
                  className="h-8 w-full border-[#6f88b4]/20 pr-7"
                />
                <Tooltip>
                  <TooltipTrigger asChild>
                    <button
                      type="button"
                      aria-label="Search syntax help"
                      className="text-muted-foreground hover:text-foreground absolute top-1/2 right-2 -translate-y-1/2 transition-colors"
                    >
                      <CircleHelp className="h-3.5 w-3.5" />
                    </button>
                  </TooltipTrigger>
                  <TooltipContent
                    side="bottom"
                    align="end"
                    className="max-w-[320px] space-y-1.5"
                  >
                    <p className="font-medium">Search syntax</p>
                    <SearchSyntaxRow
                      example="murmur x86"
                      hint="every word must match, anywhere in the name"
                    />
                    <SearchSyntaxRow
                      example={'"x86-32 conformance"'}
                      hint="exact phrase"
                    />
                    <SearchSyntaxRow
                      example="murmur OR polygon"
                      hint="either word"
                    />
                    <SearchSyntaxRow example="-no-skill" hint="exclude" />
                    <SearchSyntaxRow
                      example="tag:a OR tag:b -tag:c"
                      hint="filter by tags"
                    />
                    <p className="text-muted-foreground">
                      Matching is case-insensitive; combine freely, e.g.{" "}
                      <code className="bg-muted rounded px-1 font-mono">
                        polygon -rel tag:smoke
                      </code>
                    </p>
                  </TooltipContent>
                </Tooltip>
              </div>
              <SavedFiltersMenu
                query={searchQuery}
                onApply={(text) => setSearchQuery(text)}
              />
              <ImportDialog onImported={() => mutate()} />
            </div>
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
              <div className="bg-card/60 text-muted-foreground rounded-lg border border-dashed border-[#6f88b4]/30 px-6 py-10 text-center text-sm">
                {debouncedQuery
                  ? "No tasks match the current search."
                  : "No tasks have been created yet."}
              </div>
            ) : (
              <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                {items.map((task) => (
                  <TaskCard
                    key={task.id}
                    task={task}
                    selected={selectedCosts.has(task.id)}
                    onToggleSelected={toggleSelected}
                  />
                ))}
              </div>
            )}

            <div className="flex items-center justify-between gap-2">
              <div className="text-muted-foreground text-xs">
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
      {selectedTotal.count > 0 ? (
        <div className="bg-card/95 sticky bottom-4 z-20 mx-auto flex w-fit max-w-[95%] items-center gap-4 rounded-full border border-[#6f88b4]/40 px-5 py-2.5 shadow-lg backdrop-blur">
          <span className="text-sm font-medium">
            {selectedTotal.count} task{selectedTotal.count === 1 ? "" : "s"}{" "}
            selected
          </span>
          <span className="text-sm">
            <span className="text-muted-foreground">Total: </span>
            <span className="font-semibold tabular-nums">
              {selectedTotal.anyEstimated ? "~" : ""}
              {formatCostUsd(selectedTotal.cost)}
            </span>
          </span>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-7 px-2 text-[11px]"
            onClick={() => setSelectedCosts(new Map())}
          >
            Clear
          </Button>
        </div>
      ) : null}
    </TooltipProvider>
  );
}

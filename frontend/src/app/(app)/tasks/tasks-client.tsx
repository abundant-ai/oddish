"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
  encodeExperimentRouteParam,
  formatRelativeTime,
  formatShortDateTime,
} from "@/lib/utils";
import {
  ChevronLeft,
  ChevronRight,
  LayoutGrid,
  List,
  Loader2,
} from "lucide-react";

const PAGE_SIZE = 25;

type SortKey = "recent" | "name" | "pass" | "trials" | "version";
type ScoreBucket = "all" | "pass80" | "pass35to80" | "pass0to35" | "untested";
type ViewMode = "cards" | "list";

function passRate(task: TaskBrowseItem): number | null {
  if (!task.reward_total || task.reward_total <= 0) return null;
  return (task.reward_sum ?? task.reward_success) / task.reward_total;
}

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

function TaskCardsSkeleton() {
  return (
    <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
      {Array.from({ length: 6 }).map((_, index) => (
        <div
          key={index}
          className="rounded-lg border border-[#6f88b4]/20 bg-card/95 p-4 shadow-xs"
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
    <div className="flex flex-wrap gap-x-2 gap-y-1 text-xs text-muted-foreground">
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
        trial.error_message,
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
    } as Record<ReturnType<typeof getMatrixStatus>, number>,
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
        <div className={`text-base font-medium leading-none ${toneClass}`}>
          {avgScore == null ? "—" : `${avgScore}%`}
        </div>
        <div className="text-[11px] leading-none text-muted-foreground">
          {hasScore
            ? `${rewardSum.toFixed(2)}/${task.reward_total}`
            : "No completed trials"}
        </div>
      </div>
      {task.latest_trials.length > 0 ? (
        <div className="flex flex-wrap gap-x-2.5 gap-y-0.5 text-[10px] leading-none text-muted-foreground">
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
                <span className="font-mono text-foreground">{item.count}</span>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="text-[10px] leading-none text-muted-foreground">
          No latest-version trials
        </div>
      )}
    </div>
  );
}

function TrialGraphics({ task }: { task: TaskBrowseItem }) {
  if (task.latest_trials.length === 0) {
    return (
      <div className="rounded-md border border-dashed border-border/70 px-3 py-3 text-center text-xs text-muted-foreground">
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
          trial.error_message,
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
                className={`flex h-[18px] w-[18px] items-center justify-center rounded-[4px] border font-mono font-semibold leading-none ${config.matrixClass} ${status === "partial" ? "text-[7px] tracking-[-0.03em]" : ""}`}
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

function TaskCard({ task }: { task: TaskBrowseItem }) {
  return (
    <Card className="border-[#6f88b4]/20 bg-card/95 shadow-xs">
      <CardHeader className="space-y-2 px-5 pt-5 pb-2">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <Link
                href={`/tasks/${encodeURIComponent(task.id)}`}
                className="font-mono text-sm font-semibold text-foreground underline-offset-2 hover:underline"
              >
                {task.name}
              </Link>
              <Badge variant="outline" className="w-fit font-mono text-[11px]">
                v{task.current_version ?? "—"}
              </Badge>
              {task.tags && Object.keys(task.tags).length > 0 ? (
                <div className="flex flex-wrap gap-1">
                  {Object.entries(task.tags)
                    .filter(([k]) => k !== "github_username" && !k.startsWith("github_"))
                    .map(([k, v]) => (
                      <Link
                        key={k}
                        href={`/tasks?query=${encodeURIComponent(v)}`}
                        title={`Filter by ${k}=${v}`}
                      >
                        <Badge
                          variant="secondary"
                          className="cursor-pointer font-mono text-[10px] hover:bg-muted"
                        >
                          {k}={v}
                        </Badge>
                      </Link>
                    ))}
                </div>
              ) : null}
            </div>
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
      <CardContent className="space-y-3 px-5 pb-5">
        <div className="space-y-1.5">
          <div className="text-[11px] uppercase tracking-wide text-muted-foreground">
            Latest trials
          </div>
          <TrialGraphics task={task} />
        </div>
        <div className="grid gap-2.5 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.45fr)]">
          <div className="rounded-md border border-border/60 bg-muted/30 px-3 py-2">
            <div className="text-[11px] uppercase tracking-wide text-muted-foreground">
              Avg score
            </div>
            <div className="mt-1 text-sm font-semibold">
              <PassRateCell task={task} />
            </div>
          </div>
          <div className="rounded-md border border-border/60 bg-muted/30 px-3 py-2">
            <div className="text-[11px] uppercase tracking-wide text-muted-foreground">
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

function TaskListView({ tasks }: { tasks: TaskBrowseItem[] }) {
  return (
    <div className="overflow-x-auto rounded-sm border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Task</TableHead>
            <TableHead className="text-right">v</TableHead>
            <TableHead className="text-right">Pass rate</TableHead>
            <TableHead className="text-right">Trials</TableHead>
            <TableHead className="text-right">Last run</TableHead>
            <TableHead className="text-right">Used in</TableHead>
            <TableHead>Tags</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {tasks.map((t) => {
            const r = passRate(t);
            const pct = r == null ? null : Math.round(r * 100);
            const tone =
              pct == null
                ? "text-muted-foreground"
                : pct >= 80
                  ? "text-emerald-600 dark:text-emerald-400"
                  : pct >= 35
                    ? "text-amber-500"
                    : "text-rose-500";
            return (
              <TableRow key={t.id}>
                <TableCell>
                  <Link
                    href={`/tasks/${encodeURIComponent(t.id)}`}
                    className="font-mono text-xs hover:underline"
                  >
                    {t.name}
                  </Link>
                </TableCell>
                <TableCell className="text-right font-mono text-xs text-muted-foreground">
                  v{t.current_version ?? "?"}
                  {t.version_count > 1 ? (
                    <span className="ml-1 text-[10px]">
                      ({t.version_count})
                    </span>
                  ) : null}
                </TableCell>
                <TableCell
                  className={`text-right font-mono text-xs font-semibold ${tone}`}
                >
                  {pct == null ? "—" : `${pct}%`}
                </TableCell>
                <TableCell className="text-right font-mono text-xs">
                  {t.total_trials}
                </TableCell>
                <TableCell className="text-right text-[11px] text-muted-foreground">
                  {t.last_run_at
                    ? formatRelativeTime(t.last_run_at)
                    : "—"}
                </TableCell>
                <TableCell className="text-right font-mono text-xs">
                  {t.experiments?.length ?? 0}
                </TableCell>
                <TableCell>
                  {t.tags && Object.keys(t.tags).length > 0 ? (
                    <div className="flex flex-wrap gap-0.5">
                      {Object.entries(t.tags)
                        .filter(([k]) => !k.startsWith("github_"))
                        .slice(0, 4)
                        .map(([k, v]) => (
                          <span
                            key={k}
                            className="rounded-sm bg-muted px-1 py-0 font-mono text-[9px] text-muted-foreground"
                          >
                            {k}={v}
                          </span>
                        ))}
                    </div>
                  ) : (
                    <span className="text-[11px] text-muted-foreground">—</span>
                  )}
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </div>
  );
}

export function TasksPageClient({
  initialData,
}: {
  initialData?: TaskBrowseResponse | null;
  // ``initialQuery`` was a server-injected default; URL state is now
  // canonical so we read everything off the search params instead.
  initialQuery?: string;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  // URL is the source of truth. Read every filter from search params.
  const urlQuery = searchParams.get("q") ?? "";
  const urlSort = (searchParams.get("sort") as SortKey | null) ?? "recent";
  const urlScore =
    (searchParams.get("score") as ScoreBucket | null) ?? "all";
  const urlExperiment = searchParams.get("experiment") ?? "";
  const urlTags = useMemo(() => searchParams.getAll("tag"), [searchParams]);
  const urlView = (searchParams.get("view") as ViewMode | null) ?? "cards";
  const urlOffset = Math.max(
    0,
    parseInt(searchParams.get("offset") ?? "0", 10) || 0,
  );

  // The search box is the only field that benefits from local debouncing
  // (every keystroke shouldn't push history). All other filters write
  // immediately on click.
  const [searchDraft, setSearchDraft] = useState(urlQuery);
  const debouncedSearch = useDebouncedValue(searchDraft.trim(), 300);

  // Keep the input in sync with URL changes (e.g. browser Back).
  useEffect(() => {
    setSearchDraft(urlQuery);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlQuery]);

  // Push debounced search back to URL. Reset offset on query change so
  // pagination doesn't strand you past the new result count.
  const lastPushedSearch = useRef(urlQuery);
  useEffect(() => {
    if (debouncedSearch === lastPushedSearch.current) return;
    lastPushedSearch.current = debouncedSearch;
    const params = new URLSearchParams(searchParams.toString());
    if (debouncedSearch) params.set("q", debouncedSearch);
    else params.delete("q");
    params.delete("offset");
    router.replace(`${pathname}?${params.toString()}`, { scroll: false });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debouncedSearch]);

  const updateParam = useCallback(
    (
      key: string,
      value: string | string[] | null,
      opts: { resetOffset?: boolean } = {},
    ) => {
      const params = new URLSearchParams(searchParams.toString());
      if (value == null || value === "") {
        params.delete(key);
      } else if (Array.isArray(value)) {
        params.delete(key);
        for (const v of value) params.append(key, v);
      } else {
        params.set(key, value);
      }
      if (opts.resetOffset) params.delete("offset");
      router.replace(`${pathname}?${params.toString()}`, { scroll: false });
    },
    [router, pathname, searchParams],
  );

  const swrKey = useMemo(() => {
    const params = new URLSearchParams({
      limit: String(PAGE_SIZE),
      offset: String(urlOffset),
    });
    if (urlQuery) params.set("query", urlQuery);
    if (urlSort && urlSort !== "recent") params.set("sort", urlSort);
    if (urlScore && urlScore !== "all") params.set("score_bucket", urlScore);
    if (urlExperiment) params.set("experiment_id", urlExperiment);
    for (const chip of urlTags) params.append("tag", chip);
    return `/api/tasks/browse?${params.toString()}`;
  }, [urlOffset, urlQuery, urlSort, urlScore, urlExperiment, urlTags]);

  const { data, error, isLoading, isValidating } = useSWR<TaskBrowseResponse>(
    swrKey,
    fetcher,
    {
      refreshInterval: 60000,
      revalidateOnFocus: false,
      keepPreviousData: true,
      fallbackData:
        urlOffset === 0 &&
        !urlQuery &&
        urlSort === "recent" &&
        urlScore === "all" &&
        !urlExperiment &&
        urlTags.length === 0
          ? (initialData ?? undefined)
          : undefined,
    },
  );

  const items = data?.items ?? [];
  const hasMore = data?.has_more ?? false;
  const currentPage = Math.floor(urlOffset / PAGE_SIZE) + 1;
  const isRefreshing = !error && !isLoading && isValidating;

  // Tag/experiment chips are still derived client-side from the
  // current page; with URL-state plumbed in, hopping between pages
  // discovers more facets naturally and the user can also paste in
  // a tag chip URL we don't know about yet.
  const tagChips = useMemo(() => {
    const counts = new Map<string, number>();
    for (const t of items) {
      for (const [k, v] of Object.entries(t.tags ?? {})) {
        if (k.startsWith("github_")) continue;
        const key = `${k}=${v}`;
        counts.set(key, (counts.get(key) ?? 0) + 1);
      }
    }
    return Array.from(counts.entries())
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .slice(0, 30);
  }, [items]);

  const experimentOptions = useMemo(() => {
    const m = new Map<string, string>();
    for (const t of items) {
      for (const e of t.experiments ?? []) m.set(e.id, e.name);
    }
    return Array.from(m.entries()).sort((a, b) =>
      a[1].localeCompare(b[1]),
    );
  }, [items]);

  const tagFilter = useMemo(() => new Set(urlTags), [urlTags]);
  const toggleTag = (chip: string) => {
    const next = new Set(tagFilter);
    if (next.has(chip)) next.delete(chip);
    else next.add(chip);
    updateParam("tag", Array.from(next), { resetOffset: true });
  };

  const filtersActive =
    tagFilter.size > 0 || urlScore !== "all" || !!urlExperiment;
  const clearFilters = () => {
    const params = new URLSearchParams(searchParams.toString());
    params.delete("tag");
    params.delete("score");
    params.delete("experiment");
    params.delete("offset");
    router.replace(`${pathname}?${params.toString()}`, { scroll: false });
  };

  const scoreChips: { value: ScoreBucket; label: string }[] = [
    { value: "all", label: "All" },
    { value: "pass80", label: "≥ 80%" },
    { value: "pass35to80", label: "35–80%" },
    { value: "pass0to35", label: "< 35%" },
    { value: "untested", label: "Untested" },
  ];

  return (
    <TooltipProvider>
      <div className="space-y-6">
        <Card className="border-[#6f88b4]/20 shadow-xs">
          <CardHeader className="flex flex-col gap-3 pb-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="space-y-1">
              <CardTitle className="text-base">Recent Tasks</CardTitle>
              <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
                <span>
                  Showing {items.length}
                  {hasMore ? "+" : ""}
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
            <div className="flex flex-wrap items-center gap-2">
              <Input
                value={searchDraft}
                onChange={(event) => setSearchDraft(event.target.value)}
                placeholder="Search tasks"
                className="h-8 w-full border-[#6f88b4]/20 sm:w-[260px]"
              />
              <select
                value={urlSort}
                onChange={(e) =>
                  updateParam(
                    "sort",
                    e.target.value === "recent" ? null : e.target.value,
                    { resetOffset: true },
                  )
                }
                className="h-8 rounded-sm border bg-background px-2 font-mono text-xs"
              >
                <option value="recent">Sort: recent</option>
                <option value="name">Sort: name</option>
                <option value="pass">Sort: pass rate</option>
                <option value="trials">Sort: trials</option>
                <option value="version">Sort: versions</option>
              </select>
              {experimentOptions.length > 0 || urlExperiment ? (
                <select
                  value={urlExperiment}
                  onChange={(e) =>
                    updateParam("experiment", e.target.value || null, {
                      resetOffset: true,
                    })
                  }
                  className="h-8 max-w-[200px] truncate rounded-sm border bg-background px-2 font-mono text-xs"
                >
                  <option value="">All experiments</option>
                  {experimentOptions.map(([id, name]) => (
                    <option key={id} value={id}>
                      {name}
                    </option>
                  ))}
                </select>
              ) : null}
              <div className="inline-flex h-8 items-center overflow-hidden rounded-sm border">
                <button
                  type="button"
                  onClick={() =>
                    updateParam("view", urlView === "cards" ? null : "cards")
                  }
                  className={`flex h-full items-center gap-1 px-2 text-xs ${
                    urlView === "cards"
                      ? "bg-foreground text-background"
                      : "text-muted-foreground hover:text-foreground"
                  }`}
                  title="Card view"
                >
                  <LayoutGrid className="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  onClick={() => updateParam("view", "list")}
                  className={`flex h-full items-center gap-1 border-l px-2 text-xs ${
                    urlView === "list"
                      ? "bg-foreground text-background"
                      : "text-muted-foreground hover:text-foreground"
                  }`}
                  title="List view"
                >
                  <List className="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
              <span className="font-mono uppercase tracking-wide text-muted-foreground">
                pass rate
              </span>
              {scoreChips.map(({ value, label }) => {
                const active = urlScore === value;
                return (
                  <button
                    key={value}
                    type="button"
                    onClick={() =>
                      updateParam("score", value === "all" ? null : value, {
                        resetOffset: true,
                      })
                    }
                    className={`inline-flex items-center rounded-sm border px-1.5 py-0.5 font-mono transition ${
                      active
                        ? "border-foreground bg-foreground text-background"
                        : "border-border bg-muted/40 text-muted-foreground hover:border-foreground hover:text-foreground"
                    }`}
                  >
                    {label}
                  </button>
                );
              })}
              {tagChips.length > 0 ? (
                <>
                  <span className="ml-2 font-mono uppercase tracking-wide text-muted-foreground">
                    tags
                  </span>
                  {tagChips.map(([chip, count]) => {
                    const active = tagFilter.has(chip);
                    return (
                      <button
                        key={chip}
                        type="button"
                        onClick={() => toggleTag(chip)}
                        className={`inline-flex items-center gap-1 rounded-sm border px-1.5 py-0.5 font-mono transition ${
                          active
                            ? "border-foreground bg-foreground text-background"
                            : "border-border bg-muted/40 text-muted-foreground hover:border-foreground hover:text-foreground"
                        }`}
                      >
                        {chip}
                        <span className={active ? "opacity-70" : "opacity-60"}>
                          {count}
                        </span>
                      </button>
                    );
                  })}
                </>
              ) : null}
              {filtersActive ? (
                <button
                  type="button"
                  onClick={clearFilters}
                  className="ml-1 underline-offset-2 hover:underline"
                >
                  clear
                </button>
              ) : null}
            </div>

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
                {filtersActive
                  ? "No tasks match the current filters."
                  : urlQuery
                    ? "No tasks match the current search."
                    : "No tasks have been created yet."}
              </div>
            ) : urlView === "cards" ? (
              <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                {items.map((task) => (
                  <TaskCard key={task.id} task={task} />
                ))}
              </div>
            ) : (
              <TaskListView tasks={items} />
            )}

            <div className="flex items-center justify-between gap-2">
              <div className="text-xs text-muted-foreground">
                {items.length > 0
                  ? `${urlOffset + 1}-${urlOffset + items.length}`
                  : "0"}{" "}
                shown
              </div>
              <div className="flex items-center gap-2">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="h-8 px-3 text-[11px]"
                  onClick={() => {
                    const next = Math.max(urlOffset - PAGE_SIZE, 0);
                    updateParam(
                      "offset",
                      next === 0 ? null : String(next),
                    );
                  }}
                  disabled={urlOffset === 0 || isValidating}
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
                    updateParam("offset", String(urlOffset + PAGE_SIZE))
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

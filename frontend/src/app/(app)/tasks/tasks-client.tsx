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
import type {
  TaskAgentSummary,
  TaskBrowseItem,
  TaskBrowseResponse,
} from "@/lib/types";
import { formatRelativeTime } from "@/lib/utils";
import {
  ChevronLeft,
  ChevronRight,
  LayoutGrid,
  List,
  Loader2,
} from "lucide-react";

const DEFAULT_PAGE_SIZE = 25;
const PAGE_SIZE_OPTIONS = [10, 25, 50, 100];

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
    <div className="grid gap-4 md:grid-cols-2">
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

function passToneClass(rate: number | null): string {
  if (rate == null) return "text-muted-foreground";
  if (rate >= 0.8) return "text-emerald-600 dark:text-emerald-400";
  if (rate >= 0.35) return "text-amber-500";
  return "text-rose-500";
}

function AgentSummaryRow({
  summary,
  active,
  onClick,
}: {
  summary: TaskAgentSummary;
  active: boolean;
  onClick: () => void;
}) {
  const rate = summary.attempts > 0 ? summary.passed / summary.attempts : null;
  const pct = rate == null ? null : Math.round(rate * 100);
  const tone = passToneClass(rate);
  const barWidth = rate == null ? 0 : Math.round(rate * 100);
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          onClick={onClick}
          className={`group flex w-full items-center gap-2 rounded-sm px-1.5 py-1 text-left transition ${
            active
              ? "bg-emerald-500/10 ring-1 ring-emerald-500/40"
              : "hover:bg-muted/60"
          }`}
        >
          <span className="font-mono text-[11px] text-foreground">
            <span className="opacity-50">@</span>
            {summary.agent}
          </span>
          <span className="ml-auto flex items-center gap-2">
            <span className="h-1 w-16 overflow-hidden rounded-full bg-muted">
              <span
                className={`block h-full ${
                  rate == null
                    ? "bg-zinc-300 dark:bg-zinc-600"
                    : rate >= 0.8
                      ? "bg-emerald-500"
                      : rate >= 0.35
                        ? "bg-amber-500"
                        : "bg-rose-500"
                }`}
                style={{ width: `${barWidth}%` }}
              />
            </span>
            <span className={`font-mono text-[11px] ${tone}`}>
              {pct == null ? "—" : `${pct}%`}
            </span>
            <span className="font-mono text-[10px] text-muted-foreground">
              {summary.passed}/{summary.attempts}
            </span>
          </span>
        </button>
      </TooltipTrigger>
      <TooltipContent side="left" className="space-y-0.5 text-xs">
        <div className="font-mono text-foreground">@{summary.agent}</div>
        <div className="text-muted-foreground">
          {summary.passed} pass · {summary.attempts - summary.passed} fail
        </div>
        {summary.avg_reward != null ? (
          <div className="text-muted-foreground">
            avg reward {summary.avg_reward.toFixed(2)}
          </div>
        ) : null}
        {summary.last_run_at ? (
          <div className="text-muted-foreground">
            last run {formatRelativeTime(summary.last_run_at)}
          </div>
        ) : null}
        <div className="pt-1 text-[10px] text-muted-foreground">
          click to {active ? "clear" : "filter to"} this agent
        </div>
      </TooltipContent>
    </Tooltip>
  );
}

function TaskCard({
  task,
  activeAgent,
  onAgentClick,
}: {
  task: TaskBrowseItem;
  activeAgent: string;
  onAgentClick: (agent: string) => void;
}) {
  const summaries = task.agent_summaries ?? [];
  return (
    <Card className="border-[#6f88b4]/20 bg-card/95 shadow-xs">
      <CardHeader className="px-5 pt-5 pb-2">
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
            </div>
          </div>
          <div className="shrink-0 text-right">
            <div className="text-[11px] uppercase tracking-wide text-muted-foreground">
              Last run
            </div>
            <div className="mt-1 text-xs">
              {task.last_run_at ? formatRelativeTime(task.last_run_at) : "—"}
            </div>
          </div>
        </div>
      </CardHeader>
      <CardContent className="px-5 pb-5">
        {summaries.length === 0 ? (
          <div className="rounded-md border border-dashed border-border/60 px-3 py-3 text-center text-[11px] text-muted-foreground">
            No trials yet for v{task.current_version ?? "—"}.
          </div>
        ) : (
          <div className="flex flex-col">
            {summaries.map((summary) => (
              <AgentSummaryRow
                key={summary.agent}
                summary={summary}
                active={activeAgent === summary.agent}
                onClick={() =>
                  onAgentClick(
                    activeAgent === summary.agent ? "" : summary.agent,
                  )
                }
              />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function TaskListView({
  tasks,
  activeAgent,
  onAgentClick,
}: {
  tasks: TaskBrowseItem[];
  activeAgent: string;
  onAgentClick: (agent: string) => void;
}) {
  return (
    <div className="overflow-x-auto rounded-sm border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Task</TableHead>
            <TableHead className="text-right">v</TableHead>
            <TableHead className="text-right">Pass rate</TableHead>
            <TableHead className="text-right">Trials</TableHead>
            <TableHead>Agents</TableHead>
            <TableHead className="text-right">Last run</TableHead>
            <TableHead className="text-right">Used in</TableHead>
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
                <TableCell>
                  {(t.agent_summaries ?? []).length > 0 ? (
                    <div className="flex flex-wrap gap-0.5">
                      {(t.agent_summaries ?? []).map((s) => {
                        const active = activeAgent === s.agent;
                        const pct =
                          s.attempts > 0
                            ? Math.round((s.passed / s.attempts) * 100)
                            : null;
                        return (
                          <button
                            key={s.agent}
                            type="button"
                            onClick={() =>
                              onAgentClick(active ? "" : s.agent)
                            }
                            title={`${s.passed}/${s.attempts}${pct != null ? ` · ${pct}%` : ""}`}
                            className={`rounded-sm border px-1 py-0 font-mono text-[9px] ${
                              active
                                ? "border-emerald-500 bg-emerald-500 text-white"
                                : "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 hover:border-emerald-500 dark:text-emerald-300"
                            }`}
                          >
                            @{s.agent}
                            {pct != null ? (
                              <span className="ml-1 opacity-70">{pct}%</span>
                            ) : null}
                          </button>
                        );
                      })}
                    </div>
                  ) : (
                    <span className="text-[11px] text-muted-foreground">—</span>
                  )}
                </TableCell>
                <TableCell className="text-right text-[11px] text-muted-foreground">
                  {t.last_run_at
                    ? formatRelativeTime(t.last_run_at)
                    : "—"}
                </TableCell>
                <TableCell className="text-right font-mono text-xs">
                  {t.experiments?.length ?? 0}
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
  const urlAgent = searchParams.get("agent") ?? "";
  const urlTags = useMemo(() => searchParams.getAll("tag"), [searchParams]);
  const urlView = (searchParams.get("view") as ViewMode | null) ?? "cards";
  const urlOffset = Math.max(
    0,
    parseInt(searchParams.get("offset") ?? "0", 10) || 0,
  );
  const urlPageSize = (() => {
    const raw = parseInt(
      searchParams.get("per") ?? String(DEFAULT_PAGE_SIZE),
      10,
    );
    if (!PAGE_SIZE_OPTIONS.includes(raw)) return DEFAULT_PAGE_SIZE;
    return raw;
  })();

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
      limit: String(urlPageSize),
      offset: String(urlOffset),
    });
    if (urlQuery) params.set("query", urlQuery);
    if (urlSort && urlSort !== "recent") params.set("sort", urlSort);
    if (urlScore && urlScore !== "all") params.set("score_bucket", urlScore);
    if (urlExperiment) params.set("experiment_id", urlExperiment);
    if (urlAgent) params.set("agent", urlAgent);
    for (const chip of urlTags) params.append("tag", chip);
    return `/api/tasks/browse?${params.toString()}`;
  }, [urlOffset, urlQuery, urlSort, urlScore, urlExperiment, urlAgent, urlTags]);

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
        !urlAgent &&
        urlTags.length === 0
          ? (initialData ?? undefined)
          : undefined,
    },
  );

  const items = data?.items ?? [];
  const hasMore = data?.has_more ?? false;
  const currentPage = Math.floor(urlOffset / urlPageSize) + 1;
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

  const agentOptions = useMemo(() => {
    const s = new Set<string>();
    for (const t of items)
      for (const summary of t.agent_summaries ?? []) s.add(summary.agent);
    return Array.from(s).sort();
  }, [items]);

  const tagFilter = useMemo(() => new Set(urlTags), [urlTags]);
  const toggleTag = (chip: string) => {
    const next = new Set(tagFilter);
    if (next.has(chip)) next.delete(chip);
    else next.add(chip);
    updateParam("tag", Array.from(next), { resetOffset: true });
  };

  const filtersActive =
    tagFilter.size > 0 ||
    urlScore !== "all" ||
    !!urlExperiment ||
    !!urlAgent;
  const clearFilters = () => {
    const params = new URLSearchParams(searchParams.toString());
    params.delete("tag");
    params.delete("score");
    params.delete("experiment");
    params.delete("agent");
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
              {agentOptions.length > 0 || urlAgent ? (
                <select
                  value={urlAgent}
                  onChange={(e) =>
                    updateParam("agent", e.target.value || null, {
                      resetOffset: true,
                    })
                  }
                  className="h-8 max-w-[160px] truncate rounded-sm border bg-background px-2 font-mono text-xs"
                >
                  <option value="">All agents</option>
                  {agentOptions.map((a) => (
                    <option key={a} value={a}>
                      @{a}
                    </option>
                  ))}
                  {urlAgent && !agentOptions.includes(urlAgent) ? (
                    <option value={urlAgent}>@{urlAgent}</option>
                  ) : null}
                </select>
              ) : null}
              <select
                value={String(urlPageSize)}
                onChange={(e) =>
                  updateParam(
                    "per",
                    e.target.value === String(DEFAULT_PAGE_SIZE)
                      ? null
                      : e.target.value,
                    { resetOffset: true },
                  )
                }
                className="h-8 rounded-sm border bg-background px-2 font-mono text-xs"
                title="Tasks per page"
              >
                {PAGE_SIZE_OPTIONS.map((n) => (
                  <option key={n} value={String(n)}>
                    {n}/page
                  </option>
                ))}
              </select>
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
              <div className="grid gap-4 md:grid-cols-2">
                {items.map((task) => (
                  <TaskCard
                    key={task.id}
                    task={task}
                    activeAgent={urlAgent}
                    onAgentClick={(a) =>
                      updateParam("agent", a || null, { resetOffset: true })
                    }
                  />
                ))}
              </div>
            ) : (
              <TaskListView
                tasks={items}
                activeAgent={urlAgent}
                onAgentClick={(a) =>
                  updateParam("agent", a || null, { resetOffset: true })
                }
              />
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
                    const next = Math.max(urlOffset - urlPageSize, 0);
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
                    updateParam("offset", String(urlOffset + urlPageSize))
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

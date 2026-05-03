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
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
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
import { ChevronLeft, ChevronRight, Loader2, Plus, X } from "lucide-react";

const DEFAULT_PAGE_SIZE = 25;
const PAGE_SIZE_OPTIONS = [10, 25, 50, 100];

type SortKey = "recent" | "name" | "pass" | "trials" | "version";
type ScoreBucket = "all" | "pass80" | "pass35to80" | "pass0to35" | "untested";

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
    <div className="overflow-hidden rounded-sm border">
      {Array.from({ length: 6 }).map((_, i) => (
        <div
          key={i}
          className="flex items-center gap-4 border-b px-3 py-2 last:border-b-0"
        >
          <Skeleton className="h-3 w-44" />
          <Skeleton className="ml-auto h-3 w-12" />
          <Skeleton className="h-3 w-16" />
          <Skeleton className="h-3 w-10" />
          <Skeleton className="h-3 w-20" />
        </div>
      ))}
    </div>
  );
}

const FILTER_DIMENSIONS = [
  { id: "tag", label: "Tag" },
  { id: "agent", label: "Agent" },
  { id: "score", label: "Pass rate" },
  { id: "experiment", label: "Experiment" },
] as const;

type FilterDimension = (typeof FILTER_DIMENSIONS)[number]["id"];

function AddFilterPopover({
  tagChips,
  agentOptions,
  experimentOptions,
  onAddTag,
  onSetScore,
  onSetAgent,
  onSetExperiment,
}: {
  tagChips: [string, number][];
  agentOptions: string[];
  experimentOptions: [string, string][];
  onAddTag: (chip: string) => void;
  onSetScore: (b: ScoreBucket) => void;
  onSetAgent: (a: string) => void;
  onSetExperiment: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [dim, setDim] = useState<FilterDimension>("tag");
  const [tagDraft, setTagDraft] = useState("");
  const close = () => {
    setOpen(false);
    setDim("tag");
    setTagDraft("");
  };
  const tagSuggestions = tagChips.filter(
    ([chip]) =>
      !tagDraft || chip.toLowerCase().includes(tagDraft.trim().toLowerCase()),
  );
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="h-8 px-2 text-xs"
        >
          <Plus className="mr-1 h-3.5 w-3.5" /> Add filter
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80 p-0">
        <div className="grid grid-cols-[110px_1fr]">
          <div className="border-r bg-muted/30 py-1">
            {FILTER_DIMENSIONS.map((d) => (
              <button
                key={d.id}
                type="button"
                onClick={() => setDim(d.id)}
                className={`flex w-full items-center px-3 py-1.5 text-left text-xs ${
                  dim === d.id
                    ? "bg-background font-semibold"
                    : "text-muted-foreground hover:bg-background/60"
                }`}
              >
                {d.label}
              </button>
            ))}
          </div>
          <div className="space-y-2 p-3 text-xs">
            {dim === "tag" ? (
              <>
                <Input
                  value={tagDraft}
                  onChange={(e) => setTagDraft(e.target.value)}
                  placeholder="key=value"
                  className="h-8 font-mono"
                />
                <div className="max-h-48 space-y-0.5 overflow-y-auto">
                  {tagDraft.includes("=") ? (
                    <button
                      type="button"
                      onClick={() => {
                        onAddTag(tagDraft.trim());
                        close();
                      }}
                      className="block w-full rounded-sm bg-foreground px-2 py-1 text-left font-mono text-background"
                    >
                      use {tagDraft.trim()}
                    </button>
                  ) : null}
                  {tagSuggestions.length === 0 && !tagDraft.includes("=") ? (
                    <p className="text-muted-foreground">
                      No tags on the current page. Type a key=value pair to
                      filter directly.
                    </p>
                  ) : null}
                  {tagSuggestions.map(([chip, count]) => (
                    <button
                      key={chip}
                      type="button"
                      onClick={() => {
                        onAddTag(chip);
                        close();
                      }}
                      className="flex w-full items-center justify-between rounded-sm px-2 py-1 text-left font-mono hover:bg-muted/60"
                    >
                      <span>{chip}</span>
                      <span className="text-muted-foreground">{count}</span>
                    </button>
                  ))}
                </div>
              </>
            ) : null}
            {dim === "agent" ? (
              <div className="max-h-56 space-y-0.5 overflow-y-auto">
                {agentOptions.length === 0 ? (
                  <p className="text-muted-foreground">
                    No agents have run on the visible tasks yet.
                  </p>
                ) : null}
                {agentOptions.map((a) => (
                  <button
                    key={a}
                    type="button"
                    onClick={() => {
                      onSetAgent(a);
                      close();
                    }}
                    className="block w-full rounded-sm px-2 py-1 text-left font-mono hover:bg-muted/60"
                  >
                    @{a}
                  </button>
                ))}
              </div>
            ) : null}
            {dim === "score" ? (
              <div className="space-y-0.5">
                {(
                  [
                    ["pass80", "≥ 80% pass rate"],
                    ["pass35to80", "35–80% pass rate"],
                    ["pass0to35", "< 35% pass rate"],
                    ["untested", "Untested"],
                  ] as [ScoreBucket, string][]
                ).map(([value, label]) => (
                  <button
                    key={value}
                    type="button"
                    onClick={() => {
                      onSetScore(value);
                      close();
                    }}
                    className="block w-full rounded-sm px-2 py-1 text-left hover:bg-muted/60"
                  >
                    {label}
                  </button>
                ))}
              </div>
            ) : null}
            {dim === "experiment" ? (
              <div className="max-h-56 space-y-0.5 overflow-y-auto">
                {experimentOptions.length === 0 ? (
                  <p className="text-muted-foreground">
                    No experiments use the visible tasks yet.
                  </p>
                ) : null}
                {experimentOptions.map(([id, name]) => (
                  <button
                    key={id}
                    type="button"
                    onClick={() => {
                      onSetExperiment(id);
                      close();
                    }}
                    className="block w-full truncate rounded-sm px-2 py-1 text-left hover:bg-muted/60"
                    title={name}
                  >
                    {name}
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        </div>
      </PopoverContent>
    </Popover>
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

  const scoreLabels: Record<ScoreBucket, string> = {
    all: "All",
    pass80: "≥ 80%",
    pass35to80: "35–80%",
    pass0to35: "< 35%",
    untested: "Untested",
  };
  const experimentNameById = new Map(experimentOptions);

  // Active filters as a flat list of removable chips.
  const activeFilterChips: { key: string; label: string; onRemove: () => void }[] = [];
  for (const chip of urlTags) {
    activeFilterChips.push({
      key: `tag:${chip}`,
      label: chip,
      onRemove: () => toggleTag(chip),
    });
  }
  if (urlScore !== "all") {
    activeFilterChips.push({
      key: `score:${urlScore}`,
      label: `pass: ${scoreLabels[urlScore]}`,
      onRemove: () => updateParam("score", null, { resetOffset: true }),
    });
  }
  if (urlAgent) {
    activeFilterChips.push({
      key: `agent:${urlAgent}`,
      label: `agent: @${urlAgent}`,
      onRemove: () => updateParam("agent", null, { resetOffset: true }),
    });
  }
  if (urlExperiment) {
    activeFilterChips.push({
      key: `experiment:${urlExperiment}`,
      label: `experiment: ${experimentNameById.get(urlExperiment) ?? urlExperiment}`,
      onRemove: () => updateParam("experiment", null, { resetOffset: true }),
    });
  }

  return (
    <TooltipProvider>
      <div className="space-y-4">
        <Card className="border-[#6f88b4]/20 shadow-xs">
          <CardHeader className="flex flex-col gap-3 pb-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="space-y-1">
              <CardTitle className="text-base">Tasks</CardTitle>
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
              <AddFilterPopover
                tagChips={tagChips}
                agentOptions={agentOptions}
                experimentOptions={experimentOptions}
                onAddTag={(chip) => toggleTag(chip)}
                onSetScore={(b) =>
                  updateParam("score", b === "all" ? null : b, {
                    resetOffset: true,
                  })
                }
                onSetAgent={(a) =>
                  updateParam("agent", a || null, { resetOffset: true })
                }
                onSetExperiment={(id) =>
                  updateParam("experiment", id || null, { resetOffset: true })
                }
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
                title="Sort"
              >
                <option value="recent">Sort: recent</option>
                <option value="name">Sort: name</option>
                <option value="pass">Sort: pass rate</option>
                <option value="trials">Sort: trials</option>
                <option value="version">Sort: versions</option>
              </select>
            </div>
          </CardHeader>
          <CardContent className="space-y-3">
            {activeFilterChips.length > 0 ? (
              <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
                {activeFilterChips.map((chip) => (
                  <span
                    key={chip.key}
                    className="inline-flex items-center gap-1 rounded-sm border border-foreground bg-foreground px-1.5 py-0.5 font-mono text-background"
                  >
                    {chip.label}
                    <button
                      type="button"
                      onClick={chip.onRemove}
                      className="opacity-70 hover:opacity-100"
                      aria-label={`Remove ${chip.label}`}
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </span>
                ))}
                <button
                  type="button"
                  onClick={clearFilters}
                  className="ml-1 underline-offset-2 hover:underline"
                >
                  clear all
                </button>
              </div>
            ) : null}

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

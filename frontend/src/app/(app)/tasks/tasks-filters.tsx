"use client";

import {
  useEffect,
  useMemo,
  useState,
  type Dispatch,
  type SetStateAction,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import { useSearchParams } from "next/navigation";
import useSWR from "swr";
import { ChevronDown, FileText, Filter, Plus, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { SavedFiltersMenu } from "@/components/saved-filters-menu";
import {
  SearchSyntaxHelp,
  SearchSyntaxMultiRow,
  SearchSyntaxRow,
} from "@/components/search-syntax-help";
import { fetcher } from "@/lib/api";
import { tagColor } from "@/lib/tag-colors";
import { cn } from "@/lib/utils";
import type {
  ExperimentOptionsResponse,
  TagListResponse,
  TagSummary,
  TaskBrowseFacets,
} from "@/lib/types";
import {
  cleanOrGroups,
  COMPARE_AGG_OPTIONS,
  COMPARE_METRIC_OPTIONS,
  COMPARE_METRIC_UNIT,
  COMPARE_METRIC_WORD,
  COMPARE_MARGIN_UNIT_OPTIONS,
  COMPARE_SUBJECT_OPTIONS,
  CONDITION_DEFS,
  FILTER_DEFS,
  setTaskFilters,
  isFilterActive,
  searchParamsToFilters,
  SORT_OPTIONS,
  type CompareCond,
  type CreatedPreset,
  type FilterDef,
  type FilterValues,
  type GroupConditionDef,
  type Option,
  type OrGroup,
} from "@/lib/tasks-filters";

const ARRAY_FIELD: Record<string, keyof FilterValues> = {
  statuses: "statuses",
  priorities: "priorities",
  verdictStatuses: "verdictStatuses",
  qaOutcomes: "qaOutcomes",
  agentModels: "agentModels",
  agents: "agents",
  models: "models",
  providers: "providers",
  environments: "environments",
  trialStatuses: "trialStatuses",
  origins: "origins",
  analysisClassifications: "analysisClassifications",
  experiments: "experimentIds",
  deliveredTo: "deliveredTo",
  notDeliveredTo: "notDeliveredTo",
  categories: "categories",
};

// numrange filter key -> [min field, max field] on FilterValues.
const NUMRANGE_FIELD: Record<string, [keyof FilterValues, keyof FilterValues]> =
  {
    tokens: ["minTokens", "maxTokens"],
    steps: ["minSteps", "maxSteps"],
    trajectoryDuration: ["minDurationSeconds", "maxDurationSeconds"],
    toolCalls: ["minToolCalls", "maxToolCalls"],
    avgScore: ["avgScoreMin", "avgScoreMax"],
    totalTokens: ["totalTokensMin", "totalTokensMax"],
    runtime: ["runtimeTotalMin", "runtimeTotalMax"],
    runtimeAvg: ["runtimeAvgMin", "runtimeAvgMax"],
    passRate: ["passRateMin", "passRateMax"],
    stepsP50: ["stepsP50Min", "stepsP50Max"],
  };

// "num" (≥ N) filter key -> the single min field it writes.
const NUM_FIELD: Record<string, keyof FilterValues> = {
  minAttempts: "minAttempts",
  totalTrials: "totalTrialsMin",
  completedTrials: "completedTrialsMin",
  failedTrials: "failedTrialsMin",
  passCount: "passCountMin",
  partialCount: "partialCountMin",
  failCount: "failCountMin",
  harnessCount: "harnessCountMin",
  agentCount: "agentCountMin",
};

function optionsFor(def: FilterDef, facets: TaskBrowseFacets | null): Option[] {
  if (def.options) return def.options;
  if (def.facet && facets) {
    // A facets response cached before a vocabulary was added omits it.
    const values = (facets[def.facet] as string[] | undefined) ?? [];
    return values.map((v) => ({ value: v, label: v }));
  }
  return [];
}

const FILTER_FIELDS: Record<string, (keyof FilterValues)[]> = {
  ...Object.fromEntries(
    Object.entries(ARRAY_FIELD).map(([key, field]) => [key, [field]])
  ),
  ...NUMRANGE_FIELD,
  ...Object.fromEntries(
    Object.entries(NUM_FIELD).map(([key, field]) => [key, [field]])
  ),
  tags: ["tagsAll", "tagsAny", "tagsNone"],
  created: ["createdAfter", "createdBefore", "createdWithin"],
  trialFinished: [
    "trialFinishedAfter",
    "trialFinishedBefore",
    "trialFinishedWithin",
  ],
  reward: ["rewardMin", "rewardMax"],
  topPerformer: ["topBy", "topValue", "topMetric"],
  agentCompare: [
    "compareBy",
    "compareA",
    "compareB",
    "compareMetric",
    "compareAgg",
    "compareMargin",
    "compareMarginUnit",
  ],
  matchAny: ["orGroups"],
  ...Object.fromEntries(
    [
      "hasLink",
      "hasError",
      "hasTrajectory",
      "trialIsProbe",
      "neverDelivered",
      "sort",
      "toolNames",
      "trialMetricMatch",
    ].map((key) => [key, [key as keyof FilterValues]])
  ),
};
const EMPTY_FILTERS = searchParamsToFilters(new URLSearchParams());

// Facet vocabularies drift as trials introduce new agents, models, and
// environments, so they're not session-stable — but they needn't be
// re-asked on every remount either. Remounts inside this window reuse the
// cache silently; later ones serve it instantly and refresh in the
// background.
const FACETS_DEDUPE_MS = 5 * 60_000;

export function TasksFilters({
  searchQuery,
  onSearchChange,
  addedKeys,
  setAddedKeys,
  onClearFilters,
}: {
  searchQuery: string;
  onSearchChange: (value: string) => void;
  addedKeys: string[];
  setAddedKeys: Dispatch<SetStateAction<string[]>>;
  onClearFilters: () => void;
}) {
  // Facets load client-side so a task-grid refresh never reloads the
  // filter options; the dedupe window above keeps remounts from re-asking
  // (the 2026-08-06 HAR showed this fetch running twice per session,
  // seconds apart). The Retry below revalidates immediately regardless.
  const {
    data: facetsData,
    error: facetsError,
    isLoading: facetsLoading,
    mutate: mutateFacets,
  } = useSWR<TaskBrowseFacets>("/api/tasks/browse/facets", fetcher, {
    revalidateOnFocus: false,
    dedupingInterval: FACETS_DEDUPE_MS,
  });
  const facets = facetsData ?? null;

  const searchParams = useSearchParams();

  // Filter state lives in the URL so the grid's browse key changes (the
  // previous grid stays on screen while the next state loads) whenever a
  // filter changes — and links are shareable.
  const values = useMemo(
    () => searchParamsToFilters(new URLSearchParams(searchParams.toString())),
    [searchParams]
  );

  const [open, setOpen] = useState(false);
  const [filterSearch, setFilterSearch] = useState("");

  // Optional filters are shown when explicitly added OR already active (e.g.
  // restored from the URL on load).
  const visibleDefs = useMemo(() => {
    return FILTER_DEFS.filter(
      (def) =>
        def.key !== "sort" &&
        (!def.hidden || isFilterActive(def.key, values)) &&
        (def.pinned ||
          addedKeys.includes(def.key) ||
          isFilterActive(def.key, values))
    );
  }, [addedKeys, values]);

  const inactiveDefs = FILTER_DEFS.filter(
    (def) =>
      !def.hidden &&
      def.key !== "sort" &&
      !visibleDefs.some((v) => v.key === def.key)
  );

  const clearKey = (key: string) => {
    const fields = FILTER_FIELDS[key];
    setTaskFilters(
      Object.fromEntries(fields.map((field) => [field, EMPTY_FILTERS[field]]))
    );
    setAddedKeys((prev) => prev.filter((k) => k !== key));
  };
  const activeDefs = FILTER_DEFS.filter(
    (def) => def.key !== "sort" && isFilterActive(def.key, values)
  );
  const summary = (def: FilterDef) => {
    if (NUM_FIELD[def.key]) return `≥ ${values[NUM_FIELD[def.key]]}`;
    if (NUMRANGE_FIELD[def.key]) {
      const [min, max] = NUMRANGE_FIELD[def.key].map((field) => values[field]);
      return min !== null && max !== null
        ? `${min}–${max}`
        : min !== null
          ? `≥ ${min}`
          : `≤ ${max}`;
    }
    if (def.key === "matchAny") return `${values.orGroups?.length} groups`;
    return FILTER_FIELDS[def.key]
      .flatMap((field) => {
        const value = values[field];
        if (value === null || value === "") return [];
        const list = Array.isArray(value) ? value : [value];
        return list.map(
          (item) =>
            def.options?.find((o) => o.value === item)?.label ??
            (typeof item === "boolean" ? (item ? "Yes" : "No") : String(item))
        );
      })
      .join(", ");
  };
  return (
    <div className="space-y-3" data-testid="tasks-filters">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-56 flex-1">
          <Input
            value={searchQuery}
            onChange={(e) => onSearchChange(e.target.value)}
            placeholder="Search tasks…"
            aria-label="Search tasks"
            className="pr-8"
          />
          <SearchSyntaxHelp>
            <p className="font-medium">Search syntax</p>
            <SearchSyntaxRow
              example="node vulnerability"
              hint="every word must match"
            />
            <SearchSyntaxRow example="auth OR rbac" hint="either word" />
            <SearchSyntaxRow example={'"command exec"'} hint="exact phrase" />
            <SearchSyntaxMultiRow
              examples={["author:alice", "tag:security"]}
              hint="author or tag"
            />
          </SearchSyntaxHelp>
        </div>
        <select
          aria-label="Author"
          className="border-input bg-background h-9 rounded-md border px-3 text-sm"
          value={
            values.mine === "only" || (values.author.length === 1 && values.author[0] === "me")
              ? "me"
              : values.author.length
                ? "custom"
                : "all"
          }
          onChange={(e) =>
            setTaskFilters(current => ({
              author: e.target.value === "me" ? ["me"] : [],
              mine: current.mine === "only" ? "off" : current.mine,
            }))
          }
        >
          <option value="all">Author: Everyone</option>
          <option value="me">Author: Me</option>
          {values.author.length > 0 && !(values.author.length === 1 && values.author[0] === "me") ? (
            <option value="custom">Author: {values.author.join(", ")}</option>
          ) : null}
        </select>
        <Popover open={open} onOpenChange={setOpen}>
          <PopoverTrigger asChild>
            <Button variant="outline">
              <Filter className="mr-2 h-4 w-4" />
              Filters{activeDefs.length ? ` (${activeDefs.length})` : ""}
            </Button>
          </PopoverTrigger>
          <PopoverContent
            align="end"
            className="w-[min(440px,calc(100vw-2rem))] p-4"
            aria-label="Task filters"
          >
            <Input
              aria-label="Find a filter"
              placeholder="Find a filter…"
              value={filterSearch}
              onChange={(e) => setFilterSearch(e.target.value)}
              className="mb-4"
            />
            <div className="max-h-[60vh] space-y-4 overflow-y-auto pr-1">
              {(["Delivery", "Task", "Trial"] as const).map((group) => {
                const defs = (
                  filterSearch
                    ? FILTER_DEFS.filter((d) => !d.hidden)
                    : visibleDefs
                ).filter(
                  (d) =>
                    d.group === group &&
                    d.key !== "sort" &&
                    d.label.toLowerCase().includes(filterSearch.toLowerCase())
                );
                return defs.length ? (
                  <section key={group} className="space-y-3">
                    <h3 className="text-muted-foreground text-xs font-medium">
                      {group === "Delivery"
                        ? "Delivery history"
                        : group === "Trial"
                          ? "Trial filters"
                          : "Task filters"}
                    </h3>
                    {defs.map((def) => (
                      <FilterGroup
                        key={def.key}
                        def={def}
                        values={values}
                        set={setTaskFilters}
                        facets={facets}
                        facetsLoading={facetsLoading}
                        facetsError={Boolean(facetsError)}
                        onRetryFacets={() => mutateFacets()}
                        onRemove={
                          isFilterActive(def.key, values) ||
                          addedKeys.includes(def.key)
                            ? () => clearKey(def.key)
                            : undefined
                        }
                      />
                    ))}
                  </section>
                ) : null;
              })}
              {!filterSearch && inactiveDefs.length ? (
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button variant="outline" className="w-full">
                      <Plus className="mr-2 h-4 w-4" />
                      Add filter
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent className="max-h-72 overflow-auto">
                    {(["Task", "Delivery", "Trial"] as const).map((group) => (
                      <div key={group}>
                        <DropdownMenuLabel>{group}</DropdownMenuLabel>
                        {inactiveDefs
                          .filter((d) => d.group === group)
                          .map((def) => (
                            <DropdownMenuItem
                              key={def.key}
                              onSelect={() =>
                                setAddedKeys((prev) => [...prev, def.key])
                              }
                            >
                              {def.label}
                            </DropdownMenuItem>
                          ))}
                      </div>
                    ))}
                  </DropdownMenuContent>
                </DropdownMenu>
              ) : null}
            </div>
          </PopoverContent>
        </Popover>
        <SortControl values={values} />
        <SavedFiltersMenu />
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {activeDefs.map((def) => (
          <button
            key={def.key}
            type="button"
            onClick={() => clearKey(def.key)}
            aria-label={`Remove ${def.label} filter`}
            className="bg-muted hover:bg-muted/70 flex max-w-full items-center gap-2 rounded-md px-2.5 py-1 text-xs"
          >
            <span className="truncate">
              {def.label}: {summary(def)}
            </span>
            <X className="h-3 w-3 shrink-0" />
          </button>
        ))}
        {values.author.length || values.mine === "only" ? (
          <button
            type="button"
            className="bg-muted rounded-md px-2.5 py-1 text-xs"
            onClick={() =>
              setTaskFilters({
                author: [],
                mine: values.mine === "only" ? "off" : values.mine,
              })
            }
          >
            Author: {values.mine === "only" ? "Me" : values.author.join(", ")} ×
          </button>
        ) : null}
        {activeDefs.length ||
        searchQuery.trim() ||
        values.author.length ||
        values.mine === "only" ? (
          <Button variant="ghost" size="sm" onClick={onClearFilters}>
            Clear filters
          </Button>
        ) : null}
      </div>
    </div>
  );
}

function FilterGroup({
  def,
  values,
  set,
  facets,
  facetsLoading,
  facetsError,
  onRetryFacets,
  onRemove,
}: {
  def: FilterDef;
  values: FilterValues;
  set: (patch: Partial<FilterValues>) => void;
  facets: TaskBrowseFacets | null;
  facetsLoading: boolean;
  facetsError: boolean;
  onRetryFacets: () => void;
  onRemove?: () => void;
}) {
  return (
    <div role="group" aria-label={def.label} className="border-b border-[#6f88b4]/10 pb-3 last:border-0 last:pb-0">
      <div className="mb-1.5 flex items-center justify-between">
        <span className="text-xs font-medium">{def.label}</span>
        {onRemove ? (
          <button
            type="button"
            aria-label={`Remove ${def.label} filter`}
            className="text-muted-foreground hover:text-foreground"
            onClick={onRemove}
          >
            <X className="h-3 w-3" />
          </button>
        ) : null}
      </div>
      <FilterControl
        def={def}
        values={values}
        set={set}
        facets={facets}
        facetsLoading={facetsLoading}
        facetsError={facetsError}
        onRetryFacets={onRetryFacets}
      />
    </div>
  );
}

// Placeholder shown in place of a facet-backed control while its options load.
// Matches the h-8 control height so the sidebar layout doesn't shift on arrival.
function ControlSkeleton({ rows = 1 }: { rows?: number }) {
  return (
    <div className="space-y-1.5">
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} className="h-8 w-full" />
      ))}
    </div>
  );
}

// Shown when a facet/tag fetch fails: a short note plus a Retry that revalidates
// the relevant SWR key.
function ControlError({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="space-y-1.5">
      <p className="text-muted-foreground text-xs">Couldn’t load options.</p>
      <Button
        type="button"
        size="sm"
        variant="outline"
        className="h-7 w-full text-xs"
        onClick={onRetry}
      >
        Retry
      </Button>
    </div>
  );
}

// Controls whose options come from the /api/tasks/browse/facets fetch. These show
// a skeleton (or an error+retry) while that fetch is in flight/failed, so an
// opened dropdown never flashes a misleading "No options" during load.
function controlNeedsFacets(def: FilterDef): boolean {
  return (
    Boolean(def.facet) ||
    def.control === "agentmodel" ||
    def.control === "compare" ||
    def.control === "top"
  );
}

// Rough height of each facet-backed control, so its skeleton reserves the same
// space (single-row popover buttons vs. the taller multi-row compare/top forms).
function controlSkeletonRows(def: FilterDef): number {
  return def.control === "compare" || def.control === "top" ? 3 : 1;
}

function FilterControl({
  def,
  values,
  set,
  facets,
  facetsLoading,
  facetsError,
  onRetryFacets,
}: {
  def: FilterDef;
  values: FilterValues;
  set: (patch: Partial<FilterValues>) => void;
  facets: TaskBrowseFacets | null;
  facetsLoading: boolean;
  facetsError: boolean;
  onRetryFacets: () => void;
}) {
  if (controlNeedsFacets(def)) {
    if (facetsError && !facets) return <ControlError onRetry={onRetryFacets} />;
    if (facetsLoading && !facets)
      return <ControlSkeleton rows={controlSkeletonRows(def)} />;
  }
  switch (def.control) {
    case "multiselect":
      return (
        <MultiSelect
          options={optionsFor(def, facets)}
          field={ARRAY_FIELD[def.key]}
          values={values}
          set={set}
        />
      );
    case "select":
      return (
        <SingleSelect
          options={optionsFor(def, facets)}
          field={ARRAY_FIELD[def.key]}
          values={values}
          set={set}
        />
      );
    case "boolean":
      return <BooleanControl fieldKey={def.key} values={values} set={set} />;
    case "daterange":
      return <DateRange fieldKey={def.key} values={values} set={set} />;
    case "numrange":
      return <NumericFilter key={def.key} label={def.label} range min={values[NUMRANGE_FIELD[def.key][0]] as number | null} max={values[NUMRANGE_FIELD[def.key][1]] as number | null} presets={def.key === "stepsP50" ? [50, 100, 250] : [10, 50, 100]} onChange={(min, max) => set({[NUMRANGE_FIELD[def.key][0]]: min, [NUMRANGE_FIELD[def.key][1]]: max})} />;
    case "rewardthreshold":
      return <RewardThreshold values={values} set={set} />;
    case "compare":
      return <CompareControl values={values} set={set} facets={facets} />;
    case "top":
      return <TopPerformerControl values={values} set={set} facets={facets} />;
    case "matchany":
      return <MatchAnyControl values={values} set={set} facets={facets} />;
    case "metricmatch":
      return <MetricMatchControl values={values} set={set} />;
    case "toolnames":
      return (
        <Input
          value={values.toolNames.join(", ")}
          placeholder="bash, read_file"
          className="h-8 text-xs"
          onChange={(event) =>
            set({
              toolNames: event.target.value
                .split(",")
                .map((value) => value.trim())
                .filter(Boolean),
            })
          }
        />
      );
    case "num":
      return <NumericFilter key={def.key} label={def.label} min={values[NUM_FIELD[def.key]] as number | null} max={null} presets={def.key === "agentCount" ? [2, 3, 5] : [5, 10, 20]} onChange={min => set({[NUM_FIELD[def.key]]: min})} />;
    case "tags":
      return <TagsControl values={values} set={set} />;
    case "agentmodel":
      return <AgentModelControl values={values} set={set} facets={facets} />;
    case "experiment":
      return <ExperimentControl values={values} set={set} />;
    default:
      return null;
  }
}

function MetricMatchControl({
  values,
  set,
}: {
  values: FilterValues;
  set: (patch: Partial<FilterValues>) => void;
}) {
  const help =
    "Any trial matches when one selected-model trial meets every metric constraint. All trials requires every selected-model trial to meet every constraint.";
  return (
    <div className="grid grid-cols-2 rounded-md border p-0.5" title={help}>
      {(["any", "all"] as const).map((mode) => (
        <button
          key={mode}
          type="button"
          className={cn(
            "rounded px-2 py-1.5 text-xs capitalize",
            values.trialMetricMatch === mode &&
              "bg-primary text-primary-foreground"
          )}
          onClick={() => set({ trialMetricMatch: mode })}
        >
          {mode} trial{mode === "all" ? "s" : ""}
        </button>
      ))}
    </div>
  );
}

function pairToken(agent: string, model: string | null): string {
  return model ? `${agent}:${model}` : agent;
}

// Agent + model is the meaningful run unit (an agent AT a specific model), so we
// filter on the distinct (agent, model) pairs, grouped by agent.
function AgentModelControl({
  values,
  set,
  facets,
}: {
  values: FilterValues;
  set: (patch: Partial<FilterValues>) => void;
  facets: TaskBrowseFacets | null;
}) {
  const [search, setSearch] = useState("");
  const pairs = facets?.agent_models ?? [];
  const selected = values.agentModels;

  const toggle = (token: string) => {
    const next = selected.includes(token)
      ? selected.filter((t) => t !== token)
      : [...selected, token];
    set({ agentModels: next });
  };

  const filtered = search
    ? pairs.filter((p) =>
        `${p.agent} ${p.model ?? ""}`
          .toLowerCase()
          .includes(search.toLowerCase())
      )
    : pairs;

  const groups = new Map<string, typeof filtered>();
  for (const p of filtered) {
    const list = groups.get(p.agent) ?? [];
    list.push(p);
    groups.set(p.agent, list);
  }

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          className="h-8 w-full justify-between text-xs font-normal"
        >
          <span className="truncate">
            {selected.length === 0 ? "Any" : `${selected.length} selected`}
          </span>
          <ChevronDown className="h-3.5 w-3.5 opacity-60" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-64 p-2">
        <Input
          autoFocus
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Filter agent / model…"
          className="mb-2 h-7 text-xs"
        />
        <div className="max-h-64 space-y-1 overflow-auto">
          {groups.size === 0 ? (
            <p className="text-muted-foreground px-1 py-2 text-xs">
              No options
            </p>
          ) : (
            [...groups.entries()].map(([agent, list]) => (
              <div key={agent}>
                <p className="text-muted-foreground px-1 pt-1 text-[10px] font-semibold tracking-wide uppercase">
                  {agent}
                </p>
                {list.map((p) => {
                  const token = pairToken(p.agent, p.model);
                  return (
                    <label
                      key={token}
                      className="hover:bg-muted/60 flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 text-xs"
                    >
                      <Checkbox
                        checked={selected.includes(token)}
                        onCheckedChange={() => toggle(token)}
                      />
                      <span className="truncate">
                        {p.model ?? "(no model)"}
                      </span>
                    </label>
                  );
                })}
              </div>
            ))
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}

// Async experiment filter. Options are fetched per (debounced) search term from
// /api/tasks/browse/experiment-options instead of arriving in the facets
// payload — an org can hold 100k+ experiments, so the full list never ships.
// Selected ids are hydrated to names through the endpoint's `ids=` mode and
// pinned above the results; an id that no longer resolves (deleted experiment)
// stays visible as the raw id so it can be unchecked.
function ExperimentControl({
  values,
  set,
}: {
  values: FilterValues;
  set: (patch: Partial<FilterValues>) => void;
}) {
  const [search, setSearch] = useState("");
  const [debounced, setDebounced] = useState("");
  useEffect(() => {
    const handle = window.setTimeout(() => setDebounced(search.trim()), 300);
    return () => window.clearTimeout(handle);
  }, [search]);

  const selected = values.experimentIds;
  // keepPreviousData: while a narrower search is in flight the previous
  // results stay rendered, so the list never flashes empty between keystrokes.
  const { data, error, isLoading, mutate } = useSWR<ExperimentOptionsResponse>(
    `/api/tasks/browse/experiment-options${
      debounced ? `?query=${encodeURIComponent(debounced)}` : ""
    }`,
    fetcher,
    { revalidateOnFocus: false, keepPreviousData: true }
  );
  const { data: selectedData } = useSWR<ExperimentOptionsResponse>(
    selected.length
      ? `/api/tasks/browse/experiment-options?ids=${encodeURIComponent(
          selected.join(",")
        )}`
      : null,
    fetcher,
    { revalidateOnFocus: false }
  );

  const nameById = useMemo(() => {
    const map = new Map<string, string>();
    for (const option of selectedData?.items ?? [])
      map.set(option.id, option.name);
    for (const option of data?.items ?? []) map.set(option.id, option.name);
    return map;
  }, [data, selectedData]);

  const toggle = (id: string) =>
    set({
      experimentIds: selected.includes(id)
        ? selected.filter((v) => v !== id)
        : [...selected, id],
    });

  if (isLoading && !data) return <ControlSkeleton />;

  const results = (data?.items ?? []).filter((o) => !selected.includes(o.id));
  const label =
    selected.length === 0
      ? "Any"
      : selected.length === 1
        ? (nameById.get(selected[0]) ?? selected[0])
        : `${selected.length} selected`;

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          className="h-8 w-full justify-between text-xs font-normal"
        >
          <span className="truncate">{label}</span>
          <ChevronDown className="h-3.5 w-3.5 opacity-60" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-64 p-2">
        <Input
          autoFocus
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search experiments…"
          className="mb-2 h-7 text-xs"
        />
        <div className="max-h-56 space-y-0.5 overflow-auto">
          {selected.map((id) => (
            <label
              key={id}
              className="hover:bg-muted/60 flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 text-xs"
            >
              <Checkbox checked onCheckedChange={() => toggle(id)} />
              <span className="truncate">{nameById.get(id) ?? id}</span>
            </label>
          ))}
          {/* With keepPreviousData, post-failure `data` may belong to the
              previous query — the error replaces only the results it owns;
              chips and the input stay live, and typing retries. */}
          {error ? (
            <ControlError onRetry={() => mutate()} />
          ) : results.length === 0 && selected.length === 0 ? (
            <p className="text-muted-foreground px-1 py-2 text-xs">
              {debounced ? "No matches" : "No experiments"}
            </p>
          ) : (
            results.map((o) => (
              <label
                key={o.id}
                className="hover:bg-muted/60 flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 text-xs"
              >
                <Checkbox
                  checked={false}
                  onCheckedChange={() => toggle(o.id)}
                />
                <span className="truncate">{o.name}</span>
              </label>
            ))
          )}
        </div>
        {!error && (data?.items.length ?? 0) >= 50 ? (
          <p className="text-muted-foreground px-1 pt-1.5 text-[10px]">
            First 50 matches — keep typing to narrow
          </p>
        ) : null}
      </PopoverContent>
    </Popover>
  );
}

/** The `tag:` token form the backend expects for a tag. */
function tagToken(tag: Pick<TagSummary, "key" | "value">): string {
  return tag.value ? `${tag.key}:${tag.value}` : tag.key;
}

function TagsControl({
  values,
  set,
}: {
  values: FilterValues;
  set: (patch: Partial<FilterValues>) => void;
}) {
  // revalidateIfStale off: the shared "/api/tags" key is asked once per
  // session across this control and the dashboard's tag dropdown; tag
  // mutations and the open-gated pickers revalidate it explicitly.
  const { data, error, isLoading, mutate } = useSWR<TagListResponse>(
    "/api/tags",
    fetcher,
    { revalidateOnFocus: false, revalidateIfStale: false }
  );
  const tags = useMemo(
    () => (data?.items ?? []).filter((t) => t.state === "ACTIVE"),
    [data]
  );
  const [mode, setMode] = useState<"all" | "any" | "none">("all");
  const [search, setSearch] = useState("");

  // Mirror the facet-backed controls: skeleton while the tags fetch is in
  // flight, error + Retry on failure — never the bare "No tags" empty state
  // during load.
  if (error && !data) return <ControlError onRetry={() => mutate()} />;
  if (isLoading && !data) return <ControlSkeleton rows={2} />;

  const field: keyof FilterValues =
    mode === "all" ? "tagsAll" : mode === "any" ? "tagsAny" : "tagsNone";
  const selected = (values[field] as string[]) ?? [];
  const total =
    values.tagsAll.length + values.tagsAny.length + values.tagsNone.length;

  const toggle = (token: string) => {
    // A tag lives in at most one bucket. Strip it from all three first, then
    // add it back to the active bucket unless we're unchecking it there.
    const patch: Partial<FilterValues> = {
      tagsAll: values.tagsAll.filter((t) => t !== token),
      tagsAny: values.tagsAny.filter((t) => t !== token),
      tagsNone: values.tagsNone.filter((t) => t !== token),
    };
    if (!selected.includes(token)) {
      patch[field] = [...(patch[field] as string[]), token];
    }
    set(patch);
  };

  const filtered = search
    ? tags.filter((t) =>
        tagToken(t).toLowerCase().includes(search.toLowerCase())
      )
    : tags;

  return (
    <div className="space-y-2">
      <Segmented
        options={[
          { value: "all", label: "Has all" },
          { value: "any", label: "Has any" },
          { value: "none", label: "Has none" },
        ]}
        value={mode}
        onChange={(v) => setMode(v as "all" | "any" | "none")}
      />
      <Popover>
        <PopoverTrigger asChild>
          <Button
            variant="outline"
            size="sm"
            className="h-8 w-full justify-between text-xs font-normal"
          >
            <span className="truncate">
              {total === 0 ? "Any" : `${total} selected`}
            </span>
            <ChevronDown className="h-3.5 w-3.5 opacity-60" />
          </Button>
        </PopoverTrigger>
        <PopoverContent align="start" className="w-56 p-2">
          <Input
            autoFocus
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Filter tags…"
            className="mb-2 h-7 text-xs"
          />
          <div className="max-h-56 space-y-0.5 overflow-auto">
            {filtered.length === 0 ? (
              <p className="text-muted-foreground px-1 py-2 text-xs">No tags</p>
            ) : (
              filtered.map((t) => {
                const token = tagToken(t);
                return (
                  <label
                    key={t.id}
                    className="hover:bg-muted/60 flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 text-xs"
                  >
                    <Checkbox
                      checked={selected.includes(token)}
                      onCheckedChange={() => toggle(token)}
                    />
                    <span
                      className="h-2.5 w-2.5 shrink-0 rounded-full"
                      style={{ backgroundColor: tagColor(t.key, t.color) }}
                    />
                    <span className="truncate">{token}</span>
                    <span className="text-muted-foreground ml-auto flex shrink-0 items-center gap-1 tabular-nums">
                      <FileText className="h-3 w-3" />
                      {t.task_count}
                    </span>
                  </label>
                );
              })
            )}
          </div>
        </PopoverContent>
      </Popover>
    </div>
  );
}

function MultiSelect({
  options,
  field,
  values,
  set,
}: {
  options: Option[];
  field: keyof FilterValues;
  values: FilterValues;
  set: (patch: Partial<FilterValues>) => void;
}) {
  const [search, setSearch] = useState("");
  const selected = (values[field] as string[]) ?? [];
  const toggle = (value: string) => {
    const next = selected.includes(value)
      ? selected.filter((v) => v !== value)
      : [...selected, value];
    set({ [field]: next } as Partial<FilterValues>);
  };
  const filtered = search
    ? options.filter((o) =>
        o.label.toLowerCase().includes(search.toLowerCase())
      )
    : options;
  const label =
    selected.length === 0
      ? "Any"
      : selected.length === 1
        ? (options.find((o) => o.value === selected[0])?.label ?? selected[0])
        : `${selected.length} selected`;

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          className="h-8 w-full justify-between text-xs font-normal"
        >
          <span className="truncate">{label}</span>
          <ChevronDown className="h-3.5 w-3.5 opacity-60" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-56 p-2">
        {options.length > 8 ? (
          <Input
            autoFocus
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search…"
            className="mb-2 h-7 text-xs"
          />
        ) : null}
        <div className="max-h-56 space-y-0.5 overflow-auto">
          {filtered.length === 0 ? (
            <p className="text-muted-foreground px-1 py-2 text-xs">
              No options
            </p>
          ) : (
            filtered.map((o) => (
              <label
                key={o.value}
                className="hover:bg-muted/60 flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 text-xs"
              >
                <Checkbox
                  checked={selected.includes(o.value)}
                  onCheckedChange={() => toggle(o.value)}
                />
                <span className="truncate">{o.label}</span>
              </label>
            ))
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}

function SingleSelect({
  options,
  field,
  values,
  set,
}: {
  options: Option[];
  field: keyof FilterValues;
  values: FilterValues;
  set: (patch: Partial<FilterValues>) => void;
}) {
  const selected = (values[field] as string[]) ?? [];
  const current = selected[0] ?? "";
  const choose = (value: string) =>
    set({ [field]: value ? [value] : [] } as Partial<FilterValues>);

  return (
    <div className="space-y-0.5">
      <label className="hover:bg-muted/60 flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 text-xs">
        <input
          type="radio"
          checked={current === ""}
          onChange={() => choose("")}
        />
        <span>Any</span>
      </label>
      {options.map((o) => (
        <label
          key={o.value}
          className="hover:bg-muted/60 flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 text-xs"
        >
          <input
            type="radio"
            checked={current === o.value}
            onChange={() => choose(o.value)}
          />
          <span className="truncate">{o.label}</span>
        </label>
      ))}
    </div>
  );
}

function Segmented({
  options,
  value,
  onChange,
}: {
  options: { value: string; label: string }[];
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="flex overflow-hidden rounded-md border border-[#6f88b4]/20">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          onClick={() => onChange(o.value)}
          className={cn(
            "flex-1 px-2 py-1 text-[11px]",
            value === o.value
              ? "bg-primary/10 text-foreground"
              : "text-muted-foreground hover:bg-muted/60"
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function BooleanControl({
  fieldKey,
  values,
  set,
}: {
  fieldKey: string;
  values: FilterValues;
  set: (patch: Partial<FilterValues>) => void;
}) {
  const current = values[fieldKey as keyof FilterValues] as boolean | null;
  const value = current === null ? "any" : current ? "yes" : "no";
  return <select aria-label={FILTER_DEFS.find(def => def.key === fieldKey)?.label ?? fieldKey} value={value} className="border-input bg-background h-9 w-full rounded-md border px-3 text-sm" onChange={e => set({[fieldKey]: e.target.value === "any" ? null : e.target.value === "yes"})}>
    <option value="any">{fieldKey === "neverDelivered" ? "Any history" : "Any"}</option>
    <option value="yes">{fieldKey === "neverDelivered" ? "None recorded" : "Yes"}</option>
    <option value="no">{fieldKey === "neverDelivered" ? "Has recorded delivery" : "No"}</option>
  </select>;
}

type DateMode = "" | CreatedPreset | "custom";

// A `type="date"` picker yields a local calendar day ("YYYY-MM-DD"). Convert it
// to a UTC instant anchored to the START / END of that day in the user's local
// timezone, so the saved bound covers exactly the day they picked (no UTC shift).
function localDayStartIso(ymd: string): string {
  const [y, m, d] = ymd.split("-").map(Number);
  return new Date(y, m - 1, d, 0, 0, 0, 0).toISOString();
}
function localDayEndIso(ymd: string): string {
  const [y, m, d] = ymd.split("-").map(Number);
  return new Date(y, m - 1, d, 23, 59, 59, 999).toISOString();
}
// Format a stored instant back to the local "YYYY-MM-DD" the picker expects, so
// the input shows the same day the user chose regardless of timezone.
function isoToLocalDateInput(iso: string): string {
  const dt = new Date(iso);
  const y = dt.getFullYear();
  const m = String(dt.getMonth() + 1).padStart(2, "0");
  const d = String(dt.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

// Right-align the native calendar icon. `ml-auto` doesn't work because
// ::-webkit-datetime-edit fills the input's width, leaving no free space for an
// auto margin — so absolutely position the indicator against the right edge.
const DATE_INPUT_CLASS = cn(
  "relative h-8 w-full text-xs",
  "[&::-webkit-calendar-picker-indicator]:absolute",
  "[&::-webkit-calendar-picker-indicator]:right-2",
  "[&::-webkit-calendar-picker-indicator]:top-1/2",
  "[&::-webkit-calendar-picker-indicator]:-translate-y-1/2",
  "[&::-webkit-calendar-picker-indicator]:cursor-pointer"
);

function DateRange({
  fieldKey,
  values,
  set,
}: {
  fieldKey: string;
  values: FilterValues;
  set: (patch: Partial<FilterValues>) => void;
}) {
  const trialFinished = fieldKey === "trialFinished";
  const afterField: keyof FilterValues = trialFinished
    ? "trialFinishedAfter"
    : "createdAfter";
  const beforeField: keyof FilterValues = trialFinished
    ? "trialFinishedBefore"
    : "createdBefore";
  const withinField: keyof FilterValues = trialFinished
    ? "trialFinishedWithin"
    : "createdWithin";
  const after = values[afterField] as string | null;
  const before = values[beforeField] as string | null;
  const within = values[withinField] as CreatedPreset | null;
  const hasCustom = after !== null || before !== null;
  const hasValue = hasCustom || within !== null;
  const [mode, setMode] = useState<DateMode>(
    within ?? (hasCustom ? "custom" : "")
  );

  // Drop a stale preset highlight if the dates were cleared elsewhere (e.g.
  // "Clear all"). Custom stays open so its now-empty inputs remain visible.
  useEffect(() => {
    if (!hasValue && mode !== "custom") setMode("");
  }, [hasValue, mode]);

  const applyPreset = (key: CreatedPreset) => {
    if (mode === key) {
      setMode("");
      set({ [withinField]: null } as Partial<FilterValues>);
      return;
    }
    setMode(key);
    // Store the preset as a rolling token; the absolute custom bounds are cleared.
    set({
      [withinField]: key,
      [afterField]: null,
      [beforeField]: null,
    } as Partial<FilterValues>);
  };

  const openCustom = () => {
    setMode("custom");
    // Leaving a rolling preset for an explicit range — drop the token.
    if (within !== null) {
      set({ [withinField]: null } as Partial<FilterValues>);
    }
  };

  const tabClass = (active: boolean) =>
    cn(
      "flex-1 px-2 py-1 text-[11px]",
      active
        ? "bg-primary/10 text-foreground"
        : "text-muted-foreground hover:bg-muted/60"
    );

  // Custom From/To follow the same draft-then-apply rule as the number ranges:
  // dates are held locally (as the picker's "YYYY-MM-DD") and only committed —
  // converted to UTC day bounds — on Apply/Enter. Presets stay instant.
  const appliedDates = {
    from: after ? isoToLocalDateInput(after) : "",
    to: before ? isoToLocalDateInput(before) : "",
  };
  const commitDates = (d: { from: string; to: string }) =>
    set({
      [afterField]: d.from ? localDayStartIso(d.from) : null,
      [beforeField]: d.to ? localDayEndIso(d.to) : null,
      [withinField]: null,
    } as Partial<FilterValues>);
  const validateDates = (d: { from: string; to: string }) =>
    d.from && d.to && d.from > d.to ? "From can't be after To" : null;
  const {
    draft: dateDraft,
    setDraft: setDateDraft,
    dirty: datesDirty,
    error: dateError,
    apply: applyDates,
  } = useDraft(appliedDates, commitDates, validateDates);
  const onDateKeyDown = (e: ReactKeyboardEvent) => {
    if (e.key === "Enter") applyDates();
  };

  return (
    <div className="space-y-2">
      <div className="flex overflow-hidden rounded-md border border-[#6f88b4]/20">
        {(["24h", "7d", "30d", "90d"] as const).map((key) => (
          <button
            key={key}
            type="button"
            onClick={() => applyPreset(key)}
            className={tabClass(mode === key)}
          >
            {key}
          </button>
        ))}
        <button
          type="button"
          onClick={openCustom}
          className={tabClass(mode === "custom")}
        >
          Custom
        </button>
      </div>
      {mode === "custom" ? (
        <div className="space-y-1.5">
          <div>
            <label className="text-muted-foreground mb-0.5 block text-[11px]">
              From
            </label>
            <Input
              type="date"
              className={DATE_INPUT_CLASS}
              value={dateDraft.from}
              onChange={(e) =>
                setDateDraft({ ...dateDraft, from: e.target.value })
              }
              onKeyDown={onDateKeyDown}
            />
          </div>
          <div>
            <label className="text-muted-foreground mb-0.5 block text-[11px]">
              To
            </label>
            <Input
              type="date"
              className={DATE_INPUT_CLASS}
              value={dateDraft.to}
              onChange={(e) =>
                setDateDraft({ ...dateDraft, to: e.target.value })
              }
              onKeyDown={onDateKeyDown}
            />
          </div>
          <ApplyBar dirty={datesDirty} error={dateError} onApply={applyDates} />
        </div>
      ) : null}
    </div>
  );
}

// Aggregate sort: a plain <select> keeps this compact even with several
// options. Empty value clears the sort and restores the default recency order.
function SortControl({
  values,
}: {
  values: FilterValues;
}) {
  const pinned = values.mine === null || values.mine === "first";
  return (
    <select
      aria-label="Sort tasks"
      className="border-input bg-background h-9 max-w-64 rounded-md border px-3 text-sm"
      value={pinned ? "mine" : (values.sort ?? "recent")}
      onChange={e => setTaskFilters(current => ({
        ...(current.mine === "only" ? {author: Array.from(new Set([...current.author, "me"]))} : {}),
        mine: e.target.value === "mine" ? "first" : "off",
        sort: ["recent", "mine"].includes(e.target.value) ? null : e.target.value,
      }))}
    >
      <option value="mine">
        Sort: Mine first
        {values.sort
          ? ` · ${SORT_OPTIONS.find((o) => o.value === values.sort)?.label ?? values.sort}`
          : ""}
      </option>
      <option value="recent">Sort: Recent activity</option>
      {SORT_OPTIONS.map((o) => (
        <option key={o.value} value={o.value}>
          Sort: {o.label}
        </option>
      ))}
    </select>
  );
}

// Draft-then-apply for the free-typing filters (number ranges, count ≥ N,
// custom dates). The user's keystrokes update a LOCAL draft; nothing is written
// to the URL (and so nothing refetches) until Apply is clicked or Enter is
// pressed. `validate` blocks Apply for an invalid draft (e.g. an inverted
// min–max range) and surfaces a hint instead. Seeded from the applied value and
// re-seeded whenever that value changes externally (URL nav, Clear all, or this
// field's own Apply) — keyed on the value, not identity, so editing a different
// filter never wipes an in-progress draft here.
function useDraft<T>(
  applied: T,
  commit: (draft: T) => void,
  validate?: (draft: T) => string | null
) {
  const appliedKey = JSON.stringify(applied);
  const [draft, setDraft] = useState<T>(applied);
  useEffect(() => {
    setDraft(JSON.parse(appliedKey) as T);
  }, [appliedKey]);
  const dirty = JSON.stringify(draft) !== appliedKey;
  const error = validate ? validate(draft) : null;
  const apply = () => {
    if (!error) commit(draft);
  };
  return { draft, setDraft, dirty, error, apply };
}

// Shown only while a field has an unapplied change: the Apply button when the
// draft is valid, or an inline hint when it isn't (so an invalid range can never
// be applied).
function ApplyBar({
  dirty,
  error,
  onApply,
}: {
  dirty: boolean;
  error: string | null;
  onApply: () => void;
}) {
  if (!dirty) return null;
  if (error) {
    return <p className="mt-1.5 text-[11px] text-rose-500">{error}</p>;
  }
  return (
    <Button
      type="button"
      size="sm"
      variant="outline"
      className="mt-1.5 h-7 w-full text-xs"
      onClick={onApply}
    >
      Apply
    </Button>
  );
}


// --- Phase 2.2 "Match any of…" OR block ------------------------------------

const CONDITION_BY_ID: Record<string, GroupConditionDef> = Object.fromEntries(
  CONDITION_DEFS.map((d) => [d.id, d])
);

// Which condition rows a group shows. Stored under the UI-meta key ``_c``
// (stripped by cleanOrGroups); derived from present values on first load (e.g.
// from a shared URL).
function groupShownIds(group: OrGroup): string[] {
  const meta = group._c;
  if (Array.isArray(meta)) return meta as string[];
  return CONDITION_DEFS.filter((d) =>
    d.keys.some((k) => group[k] !== undefined)
  ).map((d) => d.id);
}

function normalizeGroups(groups: OrGroup[] | null): OrGroup[] {
  const list = groups && groups.length ? groups : [{}];
  return list.map((g) => ({ ...g, _c: groupShownIds(g) }));
}

function summarizeCondition(def: GroupConditionDef, group: OrGroup): string {
  if (def.control === "compare") {
    const c = group.compare as CompareCond | undefined;
    if (!c || !c.compare_a || !c.compare_b) return def.label;
    return `${c.compare_a} beats ${c.compare_b}`;
  }
  if (def.control === "numrange") {
    const [minK, maxK] = def.keys;
    const min = group[minK] as number | undefined;
    const max = group[maxK] as number | undefined;
    if (min != null && max != null) return `${def.label} ${min}–${max}`;
    if (min != null) return `${def.label} ≥ ${min}`;
    if (max != null) return `${def.label} ≤ ${max}`;
    return def.label;
  }
  if (def.control === "num") {
    const v = group[def.keys[0]] as number | undefined;
    return v != null ? `${def.label} ${v}` : def.label;
  }
  if (def.control === "boolean") {
    const v = group[def.keys[0]] as boolean | undefined;
    return v == null ? def.label : `${def.label}: ${v ? "yes" : "no"}`;
  }
  const arr = (group[def.keys[0]] as string[] | undefined) ?? [];
  if (!arr.length) return def.label;
  return `${def.label}: ${arr.slice(0, 2).join(", ")}${arr.length > 2 ? "…" : ""}`;
}

function groupSummary(group: OrGroup): string {
  const cleaned = cleanOrGroups([group])[0];
  if (!cleaned) return "";
  const parts: string[] = [];
  for (const def of CONDITION_DEFS) {
    if (def.keys.some((k) => cleaned[k] !== undefined)) {
      parts.push(summarizeCondition(def, cleaned));
    }
  }
  return parts.join(" and ");
}

function GroupConditionControl({
  def,
  group,
  facets,
  onField,
}: {
  def: GroupConditionDef;
  group: OrGroup;
  facets: TaskBrowseFacets | null;
  onField: (patch: Partial<FilterValues>) => void;
}) {
  const asValues = group as unknown as FilterValues;
  if (def.control === "compare") {
    return (
      <GroupCompareControl group={group} facets={facets} onField={onField} />
    );
  }
  if (def.control === "multiselect") {
    const options =
      def.options ??
      (def.facet && facets
        ? (facets[def.facet] as string[]).map((v) => ({ value: v, label: v }))
        : []);
    return (
      <MultiSelect
        options={options}
        field={def.keys[0] as keyof FilterValues}
        values={asValues}
        set={onField}
      />
    );
  }
  if (def.control === "boolean") {
    const key = def.keys[0];
    const v = group[key] as boolean | undefined;
    const cur = v === undefined ? "any" : v ? "yes" : "no";
    return (
      <Segmented
        options={[
          { value: "any", label: "Any" },
          { value: "yes", label: "Yes" },
          { value: "no", label: "No" },
        ]}
        value={cur}
        onChange={(nv) =>
          onField({
            [key]: nv === "any" ? undefined : nv === "yes",
          } as unknown as Partial<FilterValues>)
        }
      />
    );
  }
  if (def.control === "num") {
    const key = def.keys[0];
    const v = group[key] as number | undefined;
    return (
      <Input
        type="number"
        min={1}
        className="h-8 text-xs"
        placeholder="2"
        value={v ?? ""}
        onChange={(e) =>
          onField({
            [key]: e.target.value === "" ? undefined : Number(e.target.value),
          } as unknown as Partial<FilterValues>)
        }
      />
    );
  }
  const [minK, maxK] = def.keys;
  const min = group[minK] as number | undefined;
  const max = group[maxK] as number | undefined;
  const toNum = (s: string) => (s === "" ? undefined : Number(s));
  return (
    <div className="flex items-center gap-1">
      <Input
        type="number"
        className="h-8 text-xs"
        placeholder="min"
        value={min ?? ""}
        onChange={(e) =>
          onField({
            [minK]: toNum(e.target.value),
          } as unknown as Partial<FilterValues>)
        }
      />
      <span className="text-muted-foreground text-xs">–</span>
      <Input
        type="number"
        className="h-8 text-xs"
        placeholder="max"
        value={max ?? ""}
        onChange={(e) =>
          onField({
            [maxK]: toNum(e.target.value),
          } as unknown as Partial<FilterValues>)
        }
      />
    </div>
  );
}

function GroupCard({
  group,
  index,
  facets,
  onField,
  onAddCondition,
  onRemoveCondition,
  onRemoveGroup,
}: {
  group: OrGroup;
  index: number;
  facets: TaskBrowseFacets | null;
  onField: (patch: Partial<FilterValues>) => void;
  onAddCondition: (id: string) => void;
  onRemoveCondition: (id: string) => void;
  onRemoveGroup: () => void;
}) {
  const shown = groupShownIds(group);
  const available = CONDITION_DEFS.filter((d) => !shown.includes(d.id));
  return (
    <div className="border-border/70 bg-card/60 rounded-md border p-2">
      <div className="mb-1.5 flex items-center justify-between">
        <span className="text-muted-foreground text-[10px] font-semibold tracking-wide uppercase">
          Group {index + 1}
        </span>
        <button
          type="button"
          aria-label={`Remove group ${index + 1}`}
          className="text-muted-foreground hover:text-foreground"
          onClick={onRemoveGroup}
        >
          <X className="h-3 w-3" />
        </button>
      </div>
      <div className="space-y-1.5">
        {shown.map((id) => {
          const def = CONDITION_BY_ID[id];
          if (!def) return null;
          return (
            <div key={id}>
              <div className="mb-0.5 flex items-center justify-between">
                <span className="text-[11px]">{def.label}</span>
                <button
                  type="button"
                  aria-label={`Remove ${def.label}`}
                  className="text-muted-foreground hover:text-foreground"
                  onClick={() => onRemoveCondition(id)}
                >
                  <X className="h-2.5 w-2.5" />
                </button>
              </div>
              <GroupConditionControl
                def={def}
                group={group}
                facets={facets}
                onField={onField}
              />
            </div>
          );
        })}
      </div>
      {available.length ? (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              variant="outline"
              size="sm"
              className="mt-2 h-7 w-full border-dashed text-[11px]"
            >
              <Plus className="mr-1 h-3 w-3" /> Add condition
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent
            align="start"
            className="max-h-72 overflow-auto"
          >
            {available.map((d) => (
              <DropdownMenuItem
                key={d.id}
                onSelect={() => onAddCondition(d.id)}
              >
                {d.label}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      ) : null}
    </div>
  );
}

function MatchAnyControl({
  values,
  set,
  facets,
}: {
  values: FilterValues;
  set: (patch: Partial<FilterValues>) => void;
  facets: TaskBrowseFacets | null;
}) {
  const applied = normalizeGroups(values.orGroups);
  const commit = (groups: OrGroup[]) => {
    const cleaned = cleanOrGroups(groups);
    set({ orGroups: cleaned.length ? cleaned : null });
  };
  const { draft, setDraft, dirty, error, apply } = useDraft<OrGroup[]>(
    applied,
    commit
  );

  const replaceGroup = (i: number, next: OrGroup) =>
    setDraft(draft.map((g, idx) => (idx === i ? next : g)));
  const fieldSetter = (i: number) => (patch: Partial<FilterValues>) =>
    replaceGroup(i, { ...draft[i], ...(patch as unknown as OrGroup) });
  const addCondition = (i: number, id: string) =>
    replaceGroup(i, { ...draft[i], _c: [...groupShownIds(draft[i]), id] });
  const removeCondition = (i: number, id: string) => {
    const next: OrGroup = { ...draft[i] };
    const def = CONDITION_BY_ID[id];
    if (def) for (const k of def.keys) delete next[k];
    next._c = groupShownIds(draft[i]).filter((x) => x !== id);
    replaceGroup(i, next);
  };
  const addGroup = () => setDraft([...draft, { _c: [] }]);
  const removeGroup = (i: number) =>
    setDraft(
      draft.length > 1 ? draft.filter((_, idx) => idx !== i) : [{ _c: [] }]
    );

  const summaries = draft.map(groupSummary).filter(Boolean);

  return (
    <div className="space-y-2">
      <p className="text-muted-foreground text-[11px] leading-snug">
        Matches if a task fits any group. Conditions in a group are ANDed; your
        other filters still apply on top.
      </p>
      {draft.map((group, i) => (
        <div key={i}>
          {i > 0 ? (
            <div className="text-muted-foreground my-1 text-center text-[11px] font-medium">
              OR
            </div>
          ) : null}
          <GroupCard
            group={group}
            index={i}
            facets={facets}
            onField={fieldSetter(i)}
            onAddCondition={(id) => addCondition(i, id)}
            onRemoveCondition={(id) => removeCondition(i, id)}
            onRemoveGroup={() => removeGroup(i)}
          />
        </div>
      ))}
      <Button
        type="button"
        variant="outline"
        size="sm"
        className="w-full border-dashed text-[11px]"
        onClick={addGroup}
      >
        <Plus className="mr-1 h-3 w-3" /> Add OR group
      </Button>
      {summaries.length ? (
        <p className="text-muted-foreground text-[11px] leading-snug">
          Reads as:{" "}
          <span className="text-foreground">
            {summaries.map((s) => `(${s})`).join(" or ")}
          </span>
        </p>
      ) : null}
      <ApplyBar dirty={dirty} error={error} onApply={apply} />
    </div>
  );
}

type CompareDraft = {
  by: string;
  a: string;
  b: string;
  metric: string;
  agg: string;
  margin: number | null;
  unit: string;
};

const COMPARE_SELECT_CLASS =
  "border-input bg-background h-8 w-full rounded-md border px-2 text-xs";

type TopDraft = { by: string; value: string; metric: string };

// Phase 2.3 "Top performer": tasks where one subject beats every other on a
// metric. Draft-then-Apply; blocked until a subject is picked.
function TopPerformerControl({
  values,
  set,
  facets,
}: {
  values: FilterValues;
  set: (patch: Partial<FilterValues>) => void;
  facets: TaskBrowseFacets | null;
}) {
  const applied: TopDraft = {
    by: values.topBy ?? "agent",
    value: values.topValue ?? "",
    metric: values.topMetric ?? "reward",
  };
  const commit = (d: TopDraft) =>
    set({ topBy: d.by, topValue: d.value || null, topMetric: d.metric });
  const validate = (d: TopDraft) => (d.value ? null : "Pick a subject");
  const { draft, setDraft, dirty, error, apply } = useDraft(
    applied,
    commit,
    validate
  );
  const subjectLabel = draft.by === "model" ? "Model" : "Agent";
  const subjectValues =
    (draft.by === "model" ? facets?.models : facets?.agents) ?? [];
  const metricWord = COMPARE_METRIC_WORD[draft.metric] ?? draft.metric;
  return (
    <div className="space-y-2">
      <Segmented
        options={COMPARE_SUBJECT_OPTIONS}
        value={draft.by}
        onChange={(v) => setDraft({ ...draft, by: v, value: "" })}
      />
      <select
        className={COMPARE_SELECT_CLASS}
        value={draft.value}
        onChange={(e) => setDraft({ ...draft, value: e.target.value })}
      >
        <option value="">Winner {subjectLabel}…</option>
        {subjectValues.map((v) => (
          <option key={v} value={v}>
            {v}
          </option>
        ))}
      </select>
      <div>
        <label className="text-muted-foreground mb-0.5 block text-[11px]">
          On metric
        </label>
        <select
          className={COMPARE_SELECT_CLASS}
          value={draft.metric}
          onChange={(e) => setDraft({ ...draft, metric: e.target.value })}
        >
          {COMPARE_METRIC_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </div>
      {draft.value ? (
        <p className="text-muted-foreground text-[11px] leading-snug">
          {draft.value} is the top {subjectLabel.toLowerCase()} on {metricWord}
        </p>
      ) : null}
      <ApplyBar dirty={dirty} error={error} onApply={apply} />
    </div>
  );
}

// Phase 2.3 compare-in-groups: a compact "A beats B" editor that writes a nested
// ``compare`` object into an OR-group (the group's own Apply commits it).
function GroupCompareControl({
  group,
  facets,
  onField,
}: {
  group: OrGroup;
  facets: TaskBrowseFacets | null;
  onField: (patch: Partial<FilterValues>) => void;
}) {
  const c = (group.compare as CompareCond | undefined) ?? {};
  const by = c.compare_by ?? "agent";
  const subjectLabel = by === "model" ? "Model" : "Agent";
  const subjectValues =
    (by === "model" ? facets?.models : facets?.agents) ?? [];
  const update = (patch: Partial<CompareCond>) =>
    onField({
      compare: { ...c, ...patch },
    } as unknown as Partial<FilterValues>);
  return (
    <div className="space-y-1.5">
      <Segmented
        options={COMPARE_SUBJECT_OPTIONS}
        value={by}
        onChange={(v) =>
          update({ compare_by: v, compare_a: undefined, compare_b: undefined })
        }
      />
      <select
        className={COMPARE_SELECT_CLASS}
        value={c.compare_a ?? ""}
        onChange={(e) => update({ compare_a: e.target.value || undefined })}
      >
        <option value="">{subjectLabel} A…</option>
        {subjectValues.map((v) => (
          <option key={v} value={v}>
            {v}
          </option>
        ))}
      </select>
      <div className="text-muted-foreground text-center text-[11px]">beats</div>
      <select
        className={COMPARE_SELECT_CLASS}
        value={c.compare_b ?? ""}
        onChange={(e) => update({ compare_b: e.target.value || undefined })}
      >
        <option value="">{subjectLabel} B…</option>
        {subjectValues.map((v) => (
          <option key={v} value={v}>
            {v}
          </option>
        ))}
      </select>
      <select
        className={COMPARE_SELECT_CLASS}
        value={c.compare_metric ?? "reward"}
        onChange={(e) => update({ compare_metric: e.target.value })}
      >
        {COMPARE_METRIC_OPTIONS.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
      <Segmented
        options={COMPARE_AGG_OPTIONS}
        value={c.compare_agg ?? "best"}
        onChange={(v) => update({ compare_agg: v })}
      />
    </div>
  );
}

// Phase 2.1 "A beats B" compare control. Holds the whole comparison as a local
// draft and commits all seven params at once via Apply — no partial refetch
// while the user is still choosing. Apply is blocked until both sides are set
// and distinct (same guard style as the min≤max ranges).
function CompareControl({
  values,
  set,
  facets,
}: {
  values: FilterValues;
  set: (patch: Partial<FilterValues>) => void;
  facets: TaskBrowseFacets | null;
}) {
  const applied: CompareDraft = {
    by: values.compareBy ?? "agent",
    a: values.compareA ?? "",
    b: values.compareB ?? "",
    metric: values.compareMetric ?? "reward",
    agg: values.compareAgg ?? "best",
    margin: values.compareMargin,
    unit: values.compareMarginUnit ?? "pct",
  };
  const commit = (d: CompareDraft) =>
    set({
      compareBy: d.by,
      compareA: d.a || null,
      compareB: d.b || null,
      compareMetric: d.metric,
      compareAgg: d.agg,
      compareMargin: d.margin,
      compareMarginUnit: d.unit,
    });
  const validate = (d: CompareDraft) => {
    if (!d.a || !d.b) return "Pick both sides";
    if (d.a === d.b)
      return `Pick two different ${d.by === "model" ? "models" : "agents"}`;
    return null;
  };
  const { draft, setDraft, dirty, error, apply } = useDraft(
    applied,
    commit,
    validate
  );

  const subjectLabel = draft.by === "model" ? "Model" : "Agent";
  const subjectValues =
    (draft.by === "model" ? facets?.models : facets?.agents) ?? [];
  const metricWord = COMPARE_METRIC_WORD[draft.metric] ?? draft.metric;
  const metricUnit = COMPARE_METRIC_UNIT[draft.metric] ?? "";
  const showUnit = draft.unit === "abs";
  const marginText =
    draft.margin != null && !Number.isNaN(draft.margin)
      ? ` by >${draft.margin}${showUnit ? ` ${metricUnit}` : "%"}`
      : "";

  return (
    <div className="space-y-2">
      <Segmented
        options={COMPARE_SUBJECT_OPTIONS}
        value={draft.by}
        onChange={(v) => setDraft({ ...draft, by: v, a: "", b: "" })}
      />
      <select
        className={COMPARE_SELECT_CLASS}
        value={draft.a}
        onChange={(e) => setDraft({ ...draft, a: e.target.value })}
      >
        <option value="">{subjectLabel} A…</option>
        {subjectValues.map((v) => (
          <option key={v} value={v}>
            {v}
          </option>
        ))}
      </select>
      <div className="text-muted-foreground text-center text-[11px]">beats</div>
      <select
        className={COMPARE_SELECT_CLASS}
        value={draft.b}
        onChange={(e) => setDraft({ ...draft, b: e.target.value })}
      >
        <option value="">{subjectLabel} B…</option>
        {subjectValues.map((v) => (
          <option key={v} value={v}>
            {v}
          </option>
        ))}
      </select>
      <div>
        <label className="text-muted-foreground mb-0.5 block text-[11px]">
          On metric
        </label>
        <select
          className={COMPARE_SELECT_CLASS}
          value={draft.metric}
          onChange={(e) => setDraft({ ...draft, metric: e.target.value })}
        >
          {COMPARE_METRIC_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </div>
      <Segmented
        options={COMPARE_AGG_OPTIONS}
        value={draft.agg}
        onChange={(v) => setDraft({ ...draft, agg: v })}
      />
      <div className="flex items-center gap-1">
        <div className="relative flex-1">
          <Input
            type="number"
            min={0}
            className={cn("h-8 text-xs", showUnit && "pr-16")}
            placeholder="margin"
            value={draft.margin ?? ""}
            onChange={(e) =>
              setDraft({
                ...draft,
                margin: e.target.value === "" ? null : Number(e.target.value),
              })
            }
          />
          {showUnit ? (
            <span className="text-muted-foreground pointer-events-none absolute top-1/2 right-2 -translate-y-1/2 text-[10px]">
              {metricUnit}
            </span>
          ) : null}
        </div>
        <div className="w-24 shrink-0">
          <Segmented
            options={COMPARE_MARGIN_UNIT_OPTIONS}
            value={draft.unit}
            onChange={(v) => setDraft({ ...draft, unit: v })}
          />
        </div>
      </div>
      {draft.a && draft.b ? (
        <p className="text-muted-foreground text-[11px] leading-snug">
          {draft.a} beats {draft.b} on {metricWord}
          {marginText}
        </p>
      ) : null}
      <ApplyBar dirty={dirty} error={error} onApply={apply} />
    </div>
  );
}

function NumericFilter({
  label,
  min,
  max,
  range = false,
  presets,
  onChange,
}: {
  label: string;
  min: number | null;
  max: number | null;
  range?: boolean;
  presets: number[];
  onChange: (min: number | null, max: number | null) => void;
}) {
  const [custom, setCustom] = useState(false);
  const applied = { min, max };
  const { draft, setDraft, dirty, error, apply } = useDraft(
    applied,
    (d) => onChange(d.min, d.max),
    (d) =>
      [d.min, d.max].some(
        (n) =>
          n !== null &&
          (!Number.isFinite(n) || n < 0 || (!range && !Number.isInteger(n)))
      )
        ? "Enter a valid non-negative number"
        : d.min !== null && d.max !== null && d.min > d.max
          ? "Minimum cannot exceed maximum"
          : null
  );
  const value =
    custom || max !== null || (min !== null && !presets.includes(min))
      ? "custom"
      : min === null
        ? "any"
        : String(min);
  return (
    <div className="space-y-2">
      <select
        aria-label={label}
        className="border-input bg-background h-9 w-full rounded-md border px-3 text-sm"
        value={value}
        onChange={(e) => {
          const next = e.target.value;
          setCustom(next === "custom");
          if (next !== "custom")
            onChange(next === "any" ? null : Number(next), null);
        }}
      >
        <option value="any">Any</option>
        {presets.map((n) => (
          <option key={n} value={n}>
            At least {n}
          </option>
        ))}
        <option value="custom">Custom…</option>
      </select>
      {value === "custom" ? (
        <>
          <div className="flex gap-2">
            <Input
              aria-label={`${label} minimum`}
              type="number"
              min={0}
              inputMode="decimal"
              className="[appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none"
              placeholder="Minimum"
              value={draft.min ?? ""}
              onChange={(e) =>
                setDraft({
                  ...draft,
                  min: e.target.value === "" ? null : Number(e.target.value),
                })
              }
            />
            {range ? (
              <Input
                aria-label={`${label} maximum`}
                type="number"
                min={0}
                inputMode="decimal"
                className="[appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none"
                placeholder="Maximum"
                value={draft.max ?? ""}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    max: e.target.value === "" ? null : Number(e.target.value),
                  })
                }
              />
            ) : null}
          </div>
          <ApplyBar dirty={dirty} error={error} onApply={apply} />
        </>
      ) : null}
    </div>
  );
}

function RewardThreshold({
  values,
  set,
}: {
  values: FilterValues;
  set: (patch: Partial<FilterValues>) => void;
}) {
  let value = "any";
  if (values.rewardMin === 1) value = "pass";
  else if (values.rewardMax === 0) value = "fail";
  else if (values.rewardMin !== null || values.rewardMax !== null)
    value = "partial";
  const choose = (v: string) => {
    if (v === "pass") set({ rewardMin: 1, rewardMax: null });
    else if (v === "fail") set({ rewardMin: null, rewardMax: 0 });
    else if (v === "partial") set({ rewardMin: 0.01, rewardMax: 0.99 });
    else set({ rewardMin: null, rewardMax: null });
  };
  return (
    <Segmented
      options={[
        { value: "any", label: "Any" },
        { value: "pass", label: "Pass" },
        { value: "partial", label: "Partial" },
        { value: "fail", label: "Fail" },
      ]}
      value={value}
      onChange={choose}
    />
  );
}

"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import useSWR from "swr";
import { ChevronDown, FileText, Filter, Plus, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
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
  TagListResponse,
  TagSummary,
  TaskBrowseFacets,
} from "@/lib/types";
import {
  activeFilterCount,
  FILTER_DEFS,
  FILTER_PARAM_KEYS,
  filterParams,
  isFilterActive,
  searchParamsToFilters,
  type CreatedPreset,
  type FilterDef,
  type FilterValues,
  type Option,
} from "@/lib/tasks-filters";

const ARRAY_FIELD: Record<string, keyof FilterValues> = {
  statuses: "statuses",
  priorities: "priorities",
  verdictStatuses: "verdictStatuses",
  agents: "agents",
  models: "models",
  providers: "providers",
  environments: "environments",
  trialStatuses: "trialStatuses",
  origins: "origins",
  analysisClassifications: "analysisClassifications",
};

function optionsFor(def: FilterDef, facets: TaskBrowseFacets | null): Option[] {
  if (def.options) return def.options;
  if (def.facet && facets) {
    return (facets[def.facet] as string[]).map((v) => ({ value: v, label: v }));
  }
  return [];
}

export function TasksFilterSidebar() {
  // Facets are fetched client-side once so a router.refresh() of the task
  // results never reloads the filter options. revalidateOnFocus stays off and
  // there's no interval, so this loads a single time per mount.
  const { data: facetsData } = useSWR<TaskBrowseFacets>(
    "/api/tasks/browse/facets",
    fetcher,
    { revalidateOnFocus: false }
  );
  const facets = facetsData ?? null;

  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  // Filter state lives in the URL so the server-rendered results refetch (and a
  // Suspense skeleton shows) whenever a filter changes — and links are shareable.
  const values = useMemo(
    () => searchParamsToFilters(new URLSearchParams(searchParams.toString())),
    [searchParams]
  );

  const onChange = (next: FilterValues) => {
    const params = new URLSearchParams(searchParams.toString());
    for (const key of FILTER_PARAM_KEYS) params.delete(key);
    for (const [key, value] of filterParams(next)) params.set(key, value);
    params.delete("offset");
    router.replace(`${pathname}?${params.toString()}`, { scroll: false });
  };

  const set = (patch: Partial<FilterValues>) =>
    onChange({ ...values, ...patch });

  // Free-text search lives in the URL `q` param (debounced). `query` is the
  // legacy param some deep links still use — read it as a fallback.
  const urlSearch = searchParams.get("q") ?? searchParams.get("query") ?? "";
  const [searchQuery, setSearchQuery] = useState(urlSearch);

  // Keep the freshest searchParams in a ref so the debounced write reads the
  // current URL — a filter change inside the 300ms window isn't clobbered.
  const searchParamsRef = useRef(searchParams);
  searchParamsRef.current = searchParams;

  const isFirstSearchRender = useRef(true);

  // Re-sync the input when the URL search text changes externally (back/forward,
  // applying a saved filter, Clear all). No-op while the user is typing (the URL
  // already matches the trimmed input).
  useEffect(() => {
    setSearchQuery((prev) => (prev.trim() === urlSearch ? prev : urlSearch));
  }, [urlSearch]);

  useEffect(() => {
    if (isFirstSearchRender.current) {
      isFirstSearchRender.current = false;
      return;
    }
    const handle = window.setTimeout(() => {
      const params = new URLSearchParams(searchParamsRef.current.toString());
      const trimmed = searchQuery.trim();
      if (trimmed) params.set("q", trimmed);
      else params.delete("q");
      params.delete("query"); // collapse the legacy param into `q`
      params.delete("offset");
      router.replace(`${pathname}?${params.toString()}`, { scroll: false });
    }, 300);
    return () => window.clearTimeout(handle);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchQuery]);

  const [addedKeys, setAddedKeys] = useState<string[]>([]);

  // Optional filters are shown when explicitly added OR already active (e.g.
  // restored from the URL on load).
  const visibleDefs = useMemo(() => {
    return FILTER_DEFS.filter(
      (def) =>
        def.pinned ||
        addedKeys.includes(def.key) ||
        isFilterActive(def.key, values)
    );
  }, [addedKeys, values]);

  const inactiveDefs = FILTER_DEFS.filter(
    (def) => !visibleDefs.some((v) => v.key === def.key)
  );

  const clearKey = (key: string) => {
    switch (key) {
      case "created":
        set({ createdAfter: null, createdBefore: null, createdWithin: null });
        break;
      case "tokens":
        set({ minTokens: null, maxTokens: null });
        break;
      case "steps":
        set({ minSteps: null, maxSteps: null });
        break;
      case "reward":
        set({ rewardMin: null, rewardMax: null });
        break;
      case "hasLink":
      case "hasError":
      case "hasTrajectory":
      case "trialIsProbe":
        set({ [key]: null } as Partial<FilterValues>);
        break;
      case "minAttempts":
        set({ minAttempts: null });
        break;
      default:
        if (ARRAY_FIELD[key])
          set({ [ARRAY_FIELD[key]]: [] } as Partial<FilterValues>);
    }
    setAddedKeys((prev) => prev.filter((k) => k !== key));
  };

  const activeCount = activeFilterCount(values);

  return (
    <aside className="w-full shrink-0 sm:w-56">
      <div className="bg-card/95 sticky top-4 rounded-lg border border-[#6f88b4]/20 p-3 shadow-xs">
        <div className="mb-2 flex items-center justify-between">
          <span className="flex items-center gap-1.5 text-sm font-medium">
            <Filter className="h-3.5 w-3.5" />
            Filters
            {activeCount > 0 ? (
              <span className="text-muted-foreground text-[11px]">
                ({activeCount})
              </span>
            ) : null}
          </span>
          <div className="flex items-center gap-1">
            <SavedFiltersMenu />
            {activeCount > 0 || searchQuery.trim().length > 0 ? (
              <button
                type="button"
                className="text-muted-foreground hover:text-foreground text-[11px]"
                onClick={() => {
                  // Wipe every browse param (filters, tags, search, offset).
                  router.replace(pathname, { scroll: false });
                  setSearchQuery("");
                  setAddedKeys([]);
                }}
              >
                Clear all
              </button>
            ) : null}
          </div>
        </div>

        <div className="mb-3 border-b border-[#6f88b4]/10 pb-3">
          <div className="relative">
            <Input
              value={searchQuery}
              onChange={(event) => setSearchQuery(event.target.value)}
              placeholder="Search anything..."
              className="h-8 w-full border-[#6f88b4]/20 pr-7"
            />
            <SearchSyntaxHelp>
              <p className="font-medium">Search syntax</p>
              <p className="text-muted-foreground">
                Matches task name or author. Use the Tags filter below for tag
                filtering.
              </p>
              <SearchSyntaxRow
                example="node vulnerability"
                hint="every word must match (AND)"
              />
              <SearchSyntaxRow example="auth OR rbac" hint="either word (OR)" />
              <SearchSyntaxRow example={'"command exec"'} hint="exact phrase" />
              <SearchSyntaxRow example="-no-skill" hint="exclude" />
              <SearchSyntaxMultiRow
                examples={["github:alice", "author:alice", "user:alice"]}
                hint="by author — GitHub handle, email, or name"
              />
            </SearchSyntaxHelp>
          </div>
        </div>

        <div className="space-y-3">
          {visibleDefs.map((def) => (
            <FilterGroup
              key={def.key}
              def={def}
              values={values}
              set={set}
              facets={facets}
              onRemove={def.pinned ? undefined : () => clearKey(def.key)}
            />
          ))}
        </div>

        {inactiveDefs.length > 0 ? (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="outline"
                size="sm"
                className="mt-3 w-full border-dashed text-xs"
              >
                <Plus className="mr-1 h-3.5 w-3.5" />
                Add filter
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent
              align="start"
              className="z-30 max-h-80 overflow-auto"
            >
              {(["Task", "Trial"] as const).map((group) => {
                const groupDefs = inactiveDefs.filter((d) => d.group === group);
                if (!groupDefs.length) return null;
                return (
                  <div key={group}>
                    <DropdownMenuLabel className="text-muted-foreground text-[11px] uppercase">
                      {group}
                    </DropdownMenuLabel>
                    {groupDefs.map((def) => (
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
                );
              })}
            </DropdownMenuContent>
          </DropdownMenu>
        ) : null}
      </div>
    </aside>
  );
}

function FilterGroup({
  def,
  values,
  set,
  facets,
  onRemove,
}: {
  def: FilterDef;
  values: FilterValues;
  set: (patch: Partial<FilterValues>) => void;
  facets: TaskBrowseFacets | null;
  onRemove?: () => void;
}) {
  return (
    <div className="border-b border-[#6f88b4]/10 pb-3 last:border-0 last:pb-0">
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
      <FilterControl def={def} values={values} set={set} facets={facets} />
    </div>
  );
}

function FilterControl({
  def,
  values,
  set,
  facets,
}: {
  def: FilterDef;
  values: FilterValues;
  set: (patch: Partial<FilterValues>) => void;
  facets: TaskBrowseFacets | null;
}) {
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
      return <DateRange values={values} set={set} />;
    case "numrange":
      return <NumRange fieldKey={def.key} values={values} set={set} />;
    case "rewardthreshold":
      return <RewardThreshold values={values} set={set} />;
    case "num":
      return (
        <Input
          type="number"
          min={1}
          className="h-8 text-xs"
          value={values.minAttempts ?? ""}
          onChange={(e) =>
            set({
              minAttempts:
                e.target.value === "" ? null : Number(e.target.value),
            })
          }
          placeholder="2"
        />
      );
    case "tags":
      return <TagsControl values={values} set={set} />;
    case "agentmodel":
      return <AgentModelControl values={values} set={set} facets={facets} />;
    default:
      return null;
  }
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
      <PopoverContent align="start" className="z-30 w-64 p-2">
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
  const { data } = useSWR<TagListResponse>("/api/tags", fetcher, {
    revalidateOnFocus: false,
  });
  const tags = useMemo(
    () => (data?.items ?? []).filter((t) => t.state === "ACTIVE"),
    [data]
  );
  const [mode, setMode] = useState<"all" | "any" | "none">("all");
  const [search, setSearch] = useState("");

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
          { value: "all", label: "All" },
          { value: "any", label: "Any" },
          { value: "none", label: "None" },
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
        <PopoverContent align="start" className="z-30 w-56 p-2">
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
      <PopoverContent align="start" className="z-30 w-56 p-2">
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
  return (
    <Segmented
      options={[
        { value: "any", label: "Any" },
        { value: "yes", label: "Yes" },
        { value: "no", label: "No" },
      ]}
      value={value}
      onChange={(v) =>
        set({
          [fieldKey]: v === "any" ? null : v === "yes",
        } as Partial<FilterValues>)
      }
    />
  );
}

type DateMode = "" | "24h" | "7d" | "30d" | "custom";

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

function DateRange({
  values,
  set,
}: {
  values: FilterValues;
  set: (patch: Partial<FilterValues>) => void;
}) {
  const hasCustom =
    values.createdAfter !== null || values.createdBefore !== null;
  const hasValue = hasCustom || values.createdWithin !== null;
  const [mode, setMode] = useState<DateMode>(
    values.createdWithin ?? (hasCustom ? "custom" : "")
  );

  // Drop a stale preset highlight if the dates were cleared elsewhere (e.g.
  // "Clear all"). Custom stays open so its now-empty inputs remain visible.
  useEffect(() => {
    if (!hasValue && mode !== "custom") setMode("");
  }, [hasValue, mode]);

  const applyPreset = (key: CreatedPreset) => {
    if (mode === key) {
      setMode("");
      set({ createdWithin: null });
      return;
    }
    setMode(key);
    // Store the preset as a rolling token; the absolute custom bounds are cleared.
    set({ createdWithin: key, createdAfter: null, createdBefore: null });
  };

  const openCustom = () => {
    setMode("custom");
    // Leaving a rolling preset for an explicit range — drop the token.
    if (values.createdWithin !== null) set({ createdWithin: null });
  };

  const tabClass = (active: boolean) =>
    cn(
      "flex-1 px-2 py-1 text-[11px]",
      active
        ? "bg-primary/10 text-foreground"
        : "text-muted-foreground hover:bg-muted/60"
    );

  return (
    <div className="space-y-2">
      <div className="flex overflow-hidden rounded-md border border-[#6f88b4]/20">
        {(["24h", "7d", "30d"] as const).map((key) => (
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
              className="h-8 w-full text-xs [&::-webkit-calendar-picker-indicator]:ml-auto"
              value={
                values.createdAfter
                  ? isoToLocalDateInput(values.createdAfter)
                  : ""
              }
              onChange={(e) =>
                set({
                  createdAfter: e.target.value
                    ? localDayStartIso(e.target.value)
                    : null,
                })
              }
            />
          </div>
          <div>
            <label className="text-muted-foreground mb-0.5 block text-[11px]">
              To
            </label>
            <Input
              type="date"
              className="h-8 w-full text-xs [&::-webkit-calendar-picker-indicator]:ml-auto"
              value={
                values.createdBefore
                  ? isoToLocalDateInput(values.createdBefore)
                  : ""
              }
              onChange={(e) =>
                set({
                  createdBefore: e.target.value
                    ? localDayEndIso(e.target.value)
                    : null,
                })
              }
            />
          </div>
        </div>
      ) : null}
    </div>
  );
}

function NumRange({
  fieldKey,
  values,
  set,
}: {
  fieldKey: string;
  values: FilterValues;
  set: (patch: Partial<FilterValues>) => void;
}) {
  const minField = fieldKey === "tokens" ? "minTokens" : "minSteps";
  const maxField = fieldKey === "tokens" ? "maxTokens" : "maxSteps";
  const toNum = (s: string) => (s === "" ? null : Number(s));
  return (
    <div className="flex items-center gap-1">
      <Input
        type="number"
        min={0}
        className="h-8 text-xs"
        placeholder="min"
        value={(values[minField] as number | null) ?? ""}
        onChange={(e) =>
          set({ [minField]: toNum(e.target.value) } as Partial<FilterValues>)
        }
      />
      <span className="text-muted-foreground text-xs">–</span>
      <Input
        type="number"
        min={0}
        className="h-8 text-xs"
        placeholder="max"
        value={(values[maxField] as number | null) ?? ""}
        onChange={(e) =>
          set({ [maxField]: toNum(e.target.value) } as Partial<FilterValues>)
        }
      />
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

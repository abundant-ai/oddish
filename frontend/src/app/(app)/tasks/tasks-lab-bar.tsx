"use client";

import { useEffect, useState } from "react";
import { usePathname, useSearchParams } from "next/navigation";
import useSWR from "swr";
import { Rocket, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { fetcher } from "@/lib/api";
import {
  FILTER_PARAM_KEYS,
  filterParams,
  searchParamsToFilters,
  SORT_OPTIONS,
  type FilterValues,
  type MineMode,
  type Option,
} from "@/lib/tasks-filters";
import type { TaskBrowseFacets } from "@/lib/types";
import { cn } from "@/lib/utils";

// Same key and window as the sidebar's facets request, so the two share one
// SWR entry and one fetch.
const FACETS_DEDUPE_MS = 5 * 60_000;
// Select items cannot carry an empty value, so "no choice" is a sentinel
// that never collides with a customer, category, or sort token.
const ANY = "\u0000any";
// The delivery board's own rollout thresholds (min_trials 5, min_agents 3)
// plus a QA pass and the longest trajectories first: a task that clears
// this bar arrives on the board green.
const READY_PRESET: Partial<FilterValues> = {
  agentCountMin: 3,
  totalTrialsMin: 5,
  verdictStatuses: ["SUCCESS"],
  sort: "steps_p50_desc",
};
const READY_OFF: Partial<FilterValues> = {
  agentCountMin: null,
  totalTrialsMin: null,
  verdictStatuses: [],
  sort: null,
};
// Everything the bar owns; Reset clears exactly these and nothing the
// sidebar set.
const BAR_FIELDS: Partial<FilterValues> = {
  ...READY_OFF,
  author: [],
  mine: null,
  notDeliveredTo: [],
  neverDelivered: null,
  categories: [],
  stepsP50Min: null,
};
const MINE_OPTIONS: { value: MineMode; label: string; title: string }[] = [
  { value: "first", label: "Mine first", title: "Your tasks at the top" },
  { value: "only", label: "Only mine", title: "Your tasks only" },
  { value: "off", label: "Everyone", title: "No preference" },
];

// Every control writes the URL: the filters are the query string, so an
// agent or the CLI can reproduce a lab pick from the address bar alone. The
// sidebar re-seeds from the URL on external writes, so the two never fight.
export function TasksLabBar() {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const values = searchParamsToFilters(
    new URLSearchParams(searchParams.toString())
  );
  const { data: facets } = useSWR<TaskBrowseFacets>(
    "/api/tasks/browse/facets",
    fetcher,
    { revalidateOnFocus: false, dedupingInterval: FACETS_DEDUPE_MS }
  );

  const set = (patch: Partial<FilterValues>) => {
    const params = new URLSearchParams(searchParams.toString());
    for (const key of FILTER_PARAM_KEYS) params.delete(key);
    for (const [key, value] of filterParams({ ...values, ...patch })) {
      params.set(key, value);
    }
    params.delete("offset");
    const query = params.toString();
    window.history.replaceState(
      null,
      "",
      query ? `${pathname}?${query}` : pathname
    );
  };
  const single = (list: string[]) => (list.length === 1 ? list[0] : null);
  const names = (list: string[] | undefined): Option[] =>
    (list ?? []).map((value) => ({ value, label: value }));

  const qaPassed =
    values.verdictStatuses.length === 1 &&
    values.verdictStatuses[0] === "SUCCESS";
  const mine: MineMode = values.mine ?? "first";
  const ready = (Object.keys(READY_PRESET) as (keyof FilterValues)[]).every(
    (key) => JSON.stringify(values[key]) === JSON.stringify(READY_PRESET[key])
  );

  return (
    <div
      className="bg-card/95 flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-[#6f88b4]/20 px-4 py-3 text-xs shadow-xs"
      data-testid="tasks-lab-bar"
    >
      <Choice
        label="Not yet sent to"
        placeholder="Any lab"
        value={single(values.notDeliveredTo)}
        options={names(facets?.delivery_customers)}
        onChange={(lab) => set({ notDeliveredTo: lab ? [lab] : [] })}
        width="w-[150px]"
      />
      <Flag
        label="Never sent anywhere"
        checked={values.neverDelivered === true}
        onChange={(on) => set({ neverDelivered: on ? true : null })}
      />
      <Choice
        label="Category"
        placeholder="Any"
        value={single(values.categories)}
        options={names(facets?.categories)}
        onChange={(cat) => set({ categories: cat ? [cat] : [] })}
        width="w-[140px]"
      />
      <NumField
        label="Median steps ≥"
        value={values.stepsP50Min}
        onCommit={(n) => set({ stepsP50Min: n })}
      />
      <NumField
        label="Agents ≥"
        value={values.agentCountMin}
        onCommit={(n) => set({ agentCountMin: n })}
      />
      <NumField
        label="Trials ≥"
        value={values.totalTrialsMin}
        onCommit={(n) => set({ totalTrialsMin: n })}
      />
      <Flag
        label="QA passed"
        checked={qaPassed}
        onChange={(on) => set({ verdictStatuses: on ? ["SUCCESS"] : [] })}
      />
      <div
        className="flex overflow-hidden rounded-md border border-[#6f88b4]/30"
        role="group"
        aria-label="Whose tasks"
      >
        {MINE_OPTIONS.map((opt) => (
          <button
            key={opt.value}
            type="button"
            title={opt.title}
            aria-pressed={mine === opt.value}
            onClick={() =>
              set({ mine: opt.value === "first" ? null : opt.value })
            }
            className={cn(
              "px-2.5 py-1.5 transition-colors",
              mine === opt.value
                ? "bg-[#6f88b4]/20 font-medium"
                : "hover:bg-muted text-muted-foreground"
            )}
          >
            {opt.label}
          </button>
        ))}
      </div>
      <Choice
        label="Sort"
        placeholder="Recent activity"
        value={values.sort}
        options={SORT_OPTIONS}
        onChange={(sort) => set({ sort })}
        width="w-[190px]"
      />
      <div className="ml-auto flex items-center gap-2">
        <Button
          type="button"
          size="sm"
          variant={ready ? "default" : "outline"}
          className="h-8 gap-1.5 text-xs"
          aria-pressed={ready}
          title="Agents ≥ 3, trials ≥ 5, QA passed, longest trajectories first — the delivery board's own bar"
          onClick={() => set(ready ? READY_OFF : READY_PRESET)}
        >
          <Rocket className="h-3.5 w-3.5" />
          Ready to ship
        </Button>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className="h-8 gap-1.5 text-xs"
          onClick={() => set(BAR_FIELDS)}
        >
          <RotateCcw className="h-3.5 w-3.5" />
          Reset
        </Button>
      </div>
    </div>
  );
}

function Choice({
  label,
  placeholder,
  value,
  options,
  onChange,
  width,
}: {
  label: string;
  placeholder: string;
  value: string | null;
  options: Option[];
  onChange: (next: string | null) => void;
  width: string;
}) {
  return (
    <label className="flex items-center gap-2">
      <span className="text-muted-foreground whitespace-nowrap">{label}</span>
      <Select
        value={value ?? ANY}
        onValueChange={(next) => onChange(next === ANY ? null : next)}
      >
        <SelectTrigger className={cn("h-8 text-xs", width)} aria-label={label}>
          <SelectValue placeholder={placeholder} />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ANY}>{placeholder}</SelectItem>
          {options.map((opt) => (
            <SelectItem key={opt.value} value={opt.value}>
              {opt.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </label>
  );
}

function Flag({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (on: boolean) => void;
}) {
  return (
    <label className="flex items-center gap-1.5">
      <Checkbox
        checked={checked}
        onCheckedChange={(state) => onChange(state === true)}
      />
      {label}
    </label>
  );
}

// A number input that commits on blur or Enter, so typing "40" is one URL
// write and one browse fetch, not two.
function NumField({
  label,
  value,
  onCommit,
}: {
  label: string;
  value: number | null;
  onCommit: (next: number | null) => void;
}) {
  const [text, setText] = useState(value === null ? "" : String(value));
  useEffect(() => {
    setText(value === null ? "" : String(value));
  }, [value]);
  const commit = () => {
    const trimmed = text.trim();
    const next = trimmed === "" ? null : Number(trimmed);
    if (next !== null && Number.isNaN(next)) return;
    if (next !== value) onCommit(next);
  };
  return (
    <label className="flex items-center gap-2">
      <span className="text-muted-foreground whitespace-nowrap">{label}</span>
      <Input
        type="number"
        min={0}
        inputMode="numeric"
        value={text}
        onChange={(e) => setText(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") (e.target as HTMLInputElement).blur();
        }}
        className="h-8 w-16 text-xs"
        aria-label={label}
      />
    </label>
  );
}

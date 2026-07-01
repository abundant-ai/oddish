import type { TaskBrowseFacets } from "@/lib/types";

// Rolling "Created" presets. Stored in the URL as a token (e.g. created_within=24h)
// and resolved to (now - window) at query time, so the window is always relative
// to when the page is loaded/refreshed — not frozen at the moment it was clicked.
export const PRESET_MS = {
  "24h": 24 * 60 * 60 * 1000,
  "7d": 7 * 24 * 60 * 60 * 1000,
  "30d": 30 * 24 * 60 * 60 * 1000,
} as const;

export type CreatedPreset = keyof typeof PRESET_MS;

export interface FilterValues {
  statuses: string[];
  priorities: string[];
  verdictStatuses: string[];
  agents: string[];
  models: string[];
  agentModels: string[];
  providers: string[];
  environments: string[];
  trialStatuses: string[];
  origins: string[];
  analysisClassifications: string[];
  tagsAll: string[];
  tagsAny: string[];
  tagsNone: string[];
  hasLink: boolean | null;
  hasError: boolean | null;
  hasTrajectory: boolean | null;
  trialIsProbe: boolean | null;
  createdAfter: string | null; // ISO datetime (custom From)
  createdBefore: string | null; // ISO datetime (custom To)
  createdWithin: CreatedPreset | null; // rolling preset (24h/7d/30d)
  minAttempts: number | null;
  minTokens: number | null;
  maxTokens: number | null;
  minSteps: number | null;
  maxSteps: number | null;
  rewardMin: number | null;
  rewardMax: number | null;
  // Phase 1.2-lite task AGGREGATES (computed on the fly, no migration). Distinct
  // from the per-trial ranges above: these match a task's aggregate over its
  // scoped current-version trials, not a single trial's value.
  avgScoreMin: number | null; // percent 0-100
  avgScoreMax: number | null; // percent 0-100
  totalTokensMin: number | null;
  totalTokensMax: number | null;
  runtimeTotalMin: number | null; // seconds
  runtimeTotalMax: number | null; // seconds
  totalTrialsMin: number | null;
  passCountMin: number | null;
  partialCountMin: number | null;
  failCountMin: number | null;
  harnessCountMin: number | null;
  sort: string | null; // aggregate sort token (e.g. "avg_score_desc")
}

export const EMPTY_FILTERS: FilterValues = {
  statuses: [],
  priorities: [],
  verdictStatuses: [],
  agents: [],
  models: [],
  agentModels: [],
  providers: [],
  environments: [],
  trialStatuses: [],
  origins: [],
  analysisClassifications: [],
  tagsAll: [],
  tagsAny: [],
  tagsNone: [],
  hasLink: null,
  hasError: null,
  hasTrajectory: null,
  trialIsProbe: null,
  createdAfter: null,
  createdBefore: null,
  createdWithin: null,
  minAttempts: null,
  minTokens: null,
  maxTokens: null,
  minSteps: null,
  maxSteps: null,
  rewardMin: null,
  rewardMax: null,
  avgScoreMin: null,
  avgScoreMax: null,
  totalTokensMin: null,
  totalTokensMax: null,
  runtimeTotalMin: null,
  runtimeTotalMax: null,
  totalTrialsMin: null,
  passCountMin: null,
  partialCountMin: null,
  failCountMin: null,
  harnessCountMin: null,
  sort: null,
};

export interface Option {
  value: string;
  label: string;
}

// Enum-valued options are static and MUST match the stored DB form, because the
// trial-status and origin columns use `values_callable` (no name coercion):
//   - task status / priority / verdict: enum NAMES (uppercase) — default SQLEnum
//   - trial status / origin: enum VALUES (lowercase) — values_callable columns
// See db/models.py and browse_tasks_core.
export const STATUS_OPTIONS: Option[] = [
  { value: "COMPLETED", label: "Completed" },
  { value: "RUNNING", label: "Running" },
  { value: "PENDING", label: "Pending" },
  { value: "ANALYZING", label: "Analyzing" },
  { value: "VERDICT_PENDING", label: "Verdict pending" },
  { value: "FAILED", label: "Failed" },
];

export const PRIORITY_OPTIONS: Option[] = [
  { value: "HIGH", label: "High" },
  { value: "LOW", label: "Low" },
];

export const VERDICT_OPTIONS: Option[] = [
  { value: "SUCCESS", label: "Pass" },
  { value: "FAILED", label: "Fail" },
  { value: "PENDING", label: "Pending" },
];

// TrialStatus = JobStatus, stored as lowercase values via values_callable.
// (BLOCKED/CANCELLED belong to WorkerJobStatus, not trials.)
// Values are the enum *names* (uppercase): TrialModel.status uses SQLEnum without
// values_callable, so the column stores SUCCESS/FAILED/… not the lowercase enum
// values. Matches the TaskStatus filter pattern; lowercase here matches no rows.
export const TRIAL_STATUS_OPTIONS: Option[] = [
  { value: "SUCCESS", label: "Success" },
  { value: "FAILED", label: "Failed" },
  { value: "RUNNING", label: "Running" },
  { value: "QUEUED", label: "Queued" },
  { value: "RETRYING", label: "Retrying" },
];

export const ORIGIN_OPTIONS: Option[] = [
  { value: "oddish", label: "Oddish" },
  { value: "imported", label: "Imported" },
];

export type ControlKind =
  | "multiselect"
  | "select"
  | "boolean"
  | "daterange"
  | "numrange"
  | "rewardthreshold"
  | "num"
  | "tags"
  | "agentmodel"
  | "sort";

// Aggregate sort tokens (kept in sync with backend ``_AGGREGATE_SORTS``).
export const SORT_OPTIONS: Option[] = [
  { value: "avg_score_desc", label: "Avg score (high → low)" },
  { value: "avg_score_asc", label: "Avg score (low → high)" },
  { value: "total_tokens_desc", label: "Total tokens (high → low)" },
  { value: "total_tokens_asc", label: "Total tokens (low → high)" },
  { value: "runtime_total_desc", label: "Run time (long → short)" },
  { value: "runtime_total_asc", label: "Run time (short → long)" },
];

export interface FilterDef {
  key: string;
  label: string;
  group: "Task" | "Trial";
  control: ControlKind;
  options?: Option[];
  facet?: keyof TaskBrowseFacets;
  pinned?: boolean;
  // Hidden from the sidebar this is UI-only.
  hidden?: boolean;
}

// Ordered registry. `pinned` filters always render in the sidebar; the rest are
// available via "Add filter". Adding a filter the backend already supports is a
// single entry here — the control renders from `control`.
export const FILTER_DEFS: FilterDef[] = [
  {
    key: "statuses",
    label: "Status",
    group: "Task",
    control: "multiselect",
    options: STATUS_OPTIONS,
    pinned: true,
  },
  {
    key: "agentModels",
    label: "Agent · Model",
    group: "Trial",
    control: "agentmodel",
    pinned: true,
  },
  {
    key: "tags",
    label: "Tags",
    group: "Task",
    control: "tags",
    pinned: true,
  },
  {
    key: "created",
    label: "Created",
    group: "Task",
    control: "daterange",
    pinned: true,
  },
  {
    key: "environments",
    label: "Environment",
    group: "Trial",
    control: "multiselect",
    facet: "environments",
  },
  {
    key: "providers",
    label: "Provider",
    group: "Trial",
    control: "multiselect",
    facet: "providers",
  },
  {
    key: "trialStatuses",
    label: "Trial status",
    group: "Trial",
    control: "multiselect",
    options: TRIAL_STATUS_OPTIONS,
  },
  {
    key: "origins",
    label: "Origin",
    group: "Trial",
    control: "select",
    options: ORIGIN_OPTIONS,
  },
  {
    key: "priorities",
    label: "Priority",
    group: "Task",
    control: "select",
    options: PRIORITY_OPTIONS,
    hidden: true,
  },
  {
    key: "verdictStatuses",
    label: "Verdict",
    group: "Task",
    control: "select",
    options: VERDICT_OPTIONS,
    hidden: true,
  },
  {
    key: "analysisClassifications",
    label: "Analysis result",
    group: "Trial",
    control: "multiselect",
    facet: "analysis_classifications",
  },
  {
    key: "hasTrajectory",
    label: "Has trajectory",
    group: "Trial",
    control: "boolean",
    hidden: true,
  },
  { key: "hasError", label: "Has error", group: "Trial", control: "boolean" },
  {
    key: "hasLink",
    label: "Has source link",
    group: "Task",
    control: "boolean",
    hidden: true,
  },
  {
    key: "tokens",
    label: "Token size",
    group: "Trial",
    control: "numrange",
  },
  {
    key: "steps",
    label: "Trajectory length",
    group: "Trial",
    control: "numrange",
  },
  {
    key: "reward",
    label: "Reward",
    group: "Trial",
    control: "rewardthreshold",
  },
  {
    key: "minAttempts",
    label: "Retried (attempts ≥)",
    group: "Trial",
    control: "num",
  },
  {
    key: "trialIsProbe",
    label: "Only probe trials",
    group: "Trial",
    control: "boolean",
    hidden: true,
  },
  // Phase 1.2-lite aggregate filters/sort (task-level rollups over the scoped
  // current-version trials). Sort is pinned so it's always reachable.
  {
    key: "sort",
    label: "Sort by",
    group: "Task",
    control: "sort",
    pinned: true,
  },
  {
    key: "avgScore",
    label: "Avg score %",
    group: "Task",
    control: "numrange",
  },
  {
    key: "totalTokens",
    label: "Total tokens (task)",
    group: "Task",
    control: "numrange",
  },
  {
    key: "runtime",
    label: "Run time (s, task)",
    group: "Task",
    control: "numrange",
  },
  {
    key: "totalTrials",
    label: "Total trials ≥",
    group: "Task",
    control: "num",
  },
  {
    key: "passCount",
    label: "Pass count ≥",
    group: "Task",
    control: "num",
  },
  {
    key: "partialCount",
    label: "Partial count ≥",
    group: "Task",
    control: "num",
  },
  {
    key: "failCount",
    label: "Fail count ≥",
    group: "Task",
    control: "num",
  },
  {
    key: "harnessCount",
    label: "Harness count ≥",
    group: "Task",
    control: "num",
  },
];

// Whether a registry filter currently holds a value (drives the active count,
// the chip summary, and which optional filters show as added).
export function isFilterActive(key: string, f: FilterValues): boolean {
  switch (key) {
    case "statuses":
      return f.statuses.length > 0;
    case "priorities":
      return f.priorities.length > 0;
    case "verdictStatuses":
      return f.verdictStatuses.length > 0;
    case "agents":
      return f.agents.length > 0;
    case "models":
      return f.models.length > 0;
    case "agentModels":
      return f.agentModels.length > 0;
    case "providers":
      return f.providers.length > 0;
    case "environments":
      return f.environments.length > 0;
    case "trialStatuses":
      return f.trialStatuses.length > 0;
    case "origins":
      return f.origins.length > 0;
    case "analysisClassifications":
      return f.analysisClassifications.length > 0;
    case "tags":
      return (
        f.tagsAll.length > 0 || f.tagsAny.length > 0 || f.tagsNone.length > 0
      );
    case "hasLink":
      return f.hasLink !== null;
    case "hasError":
      return f.hasError !== null;
    case "hasTrajectory":
      return f.hasTrajectory !== null;
    case "trialIsProbe":
      return f.trialIsProbe !== null;
    case "created":
      return (
        f.createdAfter !== null ||
        f.createdBefore !== null ||
        f.createdWithin !== null
      );
    case "minAttempts":
      return f.minAttempts !== null;
    case "tokens":
      return f.minTokens !== null || f.maxTokens !== null;
    case "steps":
      return f.minSteps !== null || f.maxSteps !== null;
    case "reward":
      return f.rewardMin !== null || f.rewardMax !== null;
    case "avgScore":
      return f.avgScoreMin !== null || f.avgScoreMax !== null;
    case "totalTokens":
      return f.totalTokensMin !== null || f.totalTokensMax !== null;
    case "runtime":
      return f.runtimeTotalMin !== null || f.runtimeTotalMax !== null;
    case "totalTrials":
      return f.totalTrialsMin !== null;
    case "passCount":
      return f.passCountMin !== null;
    case "partialCount":
      return f.partialCountMin !== null;
    case "failCount":
      return f.failCountMin !== null;
    case "harnessCount":
      return f.harnessCountMin !== null;
    case "sort":
      return f.sort !== null;
    default:
      return false;
  }
}

export function activeFilterCount(f: FilterValues): number {
  return FILTER_DEFS.filter((def) => isFilterActive(def.key, f)).length;
}

// Serialize active filters into /tasks/browse query params. Lists are CSV;
// dates become a full-day ISO bound; booleans become "true"/"false".
export function filterParams(f: FilterValues): [string, string][] {
  const out: [string, string][] = [];
  const csv = (param: string, vals: string[]) => {
    if (vals.length) out.push([param, vals.join(",")]);
  };
  const bool = (param: string, v: boolean | null) => {
    if (v !== null) out.push([param, v ? "true" : "false"]);
  };
  const num = (param: string, v: number | null) => {
    if (v !== null && !Number.isNaN(v)) out.push([param, String(v)]);
  };

  csv("statuses", f.statuses);
  csv("priorities", f.priorities);
  csv("verdict_statuses", f.verdictStatuses);
  csv("agents", f.agents);
  csv("models", f.models);
  csv("agent_models", f.agentModels);
  csv("providers", f.providers);
  csv("environments", f.environments);
  csv("trial_statuses", f.trialStatuses);
  csv("origins", f.origins);
  csv("analysis_classifications", f.analysisClassifications);
  csv("tags", f.tagsAll);
  csv("tags_any", f.tagsAny);
  csv("tags_none", f.tagsNone);
  bool("has_link", f.hasLink);
  bool("has_error", f.hasError);
  bool("has_trajectory", f.hasTrajectory);
  bool("trial_is_probe", f.trialIsProbe);
  if (f.createdAfter) out.push(["created_after", f.createdAfter]);
  if (f.createdBefore) out.push(["created_before", f.createdBefore]);
  if (f.createdWithin) out.push(["created_within", f.createdWithin]);
  num("min_attempts", f.minAttempts);
  num("min_tokens", f.minTokens);
  num("max_tokens", f.maxTokens);
  num("min_steps", f.minSteps);
  num("max_steps", f.maxSteps);
  num("reward_min", f.rewardMin);
  num("reward_max", f.rewardMax);
  num("avg_score_min", f.avgScoreMin);
  num("avg_score_max", f.avgScoreMax);
  num("total_tokens_min", f.totalTokensMin);
  num("total_tokens_max", f.totalTokensMax);
  num("runtime_total_min", f.runtimeTotalMin);
  num("runtime_total_max", f.runtimeTotalMax);
  num("total_trials_min", f.totalTrialsMin);
  num("pass_count_min", f.passCountMin);
  num("partial_count_min", f.partialCountMin);
  num("fail_count_min", f.failCountMin);
  num("harness_count_min", f.harnessCountMin);
  if (f.sort) out.push(["sort", f.sort]);
  return out;
}

// Tasks per browse page. 24 fills the 3-column tile grid evenly (8 rows).
export const TASKS_PAGE_SIZE = 24;

// Backend query-param keys the filters serialize to (also the URL keys). Used
// to clear stale filter params before re-writing, and to forward them to the
// browse fetch.
export const FILTER_PARAM_KEYS = [
  "statuses",
  "priorities",
  "verdict_statuses",
  "agents",
  "models",
  "agent_models",
  "providers",
  "environments",
  "trial_statuses",
  "origins",
  "analysis_classifications",
  "tags",
  "tags_any",
  "tags_none",
  "has_link",
  "has_error",
  "has_trajectory",
  "trial_is_probe",
  "created_after",
  "created_before",
  "created_within",
  "min_attempts",
  "min_tokens",
  "max_tokens",
  "min_steps",
  "max_steps",
  "reward_min",
  "reward_max",
  "avg_score_min",
  "avg_score_max",
  "total_tokens_min",
  "total_tokens_max",
  "runtime_total_min",
  "runtime_total_max",
  "total_trials_min",
  "pass_count_min",
  "partial_count_min",
  "fail_count_min",
  "harness_count_min",
  "sort",
] as const;

// Backend filter params that have no sidebar control yet but are still valid on
// /tasks/browse (set via deep links or saved filters). The server results
// loader forwards these in addition to FILTER_PARAM_KEYS so they aren't
// silently dropped. They're intentionally NOT in FILTER_PARAM_KEYS so the
// sidebar's clear-on-change loop doesn't wipe deep-linked values.
export const EXTRA_BROWSE_PARAM_KEYS = [
  "experiment_ids",
  "run_analysis",
  "run_probe",
  "harbor_shas",
  "harbor_stages",
  // Phase 1.2-lite aggregate params with no sidebar control yet (deep-linkable).
  "completed_trials_min",
  "failed_trials_min",
  "runtime_avg_min",
  "runtime_avg_max",
] as const;

// Everything the browse fetch should forward / saved filters should capture.
export const BROWSE_FORWARD_KEYS = [
  ...FILTER_PARAM_KEYS,
  ...EXTRA_BROWSE_PARAM_KEYS,
] as const;

// Inverse of filterParams: read filter values back out of URL search params so
// the sidebar controls can seed from the URL.
export function searchParamsToFilters(sp: URLSearchParams): FilterValues {
  const csv = (k: string) => {
    const v = sp.get(k);
    return v ? v.split(",").filter(Boolean) : [];
  };
  const bool = (k: string) => {
    const v = sp.get(k);
    return v === null ? null : v === "true";
  };
  const num = (k: string) => {
    const v = sp.get(k);
    return v === null || v === "" ? null : Number(v);
  };
  return {
    statuses: csv("statuses"),
    priorities: csv("priorities"),
    verdictStatuses: csv("verdict_statuses"),
    agents: csv("agents"),
    models: csv("models"),
    agentModels: csv("agent_models"),
    providers: csv("providers"),
    environments: csv("environments"),
    trialStatuses: csv("trial_statuses"),
    origins: csv("origins"),
    analysisClassifications: csv("analysis_classifications"),
    tagsAll: csv("tags"),
    tagsAny: csv("tags_any"),
    tagsNone: csv("tags_none"),
    hasLink: bool("has_link"),
    hasError: bool("has_error"),
    hasTrajectory: bool("has_trajectory"),
    trialIsProbe: bool("trial_is_probe"),
    createdAfter: sp.get("created_after"),
    createdBefore: sp.get("created_before"),
    createdWithin: ((): CreatedPreset | null => {
      const v = sp.get("created_within");
      return v && v in PRESET_MS ? (v as CreatedPreset) : null;
    })(),
    minAttempts: num("min_attempts"),
    minTokens: num("min_tokens"),
    maxTokens: num("max_tokens"),
    minSteps: num("min_steps"),
    maxSteps: num("max_steps"),
    rewardMin: num("reward_min"),
    rewardMax: num("reward_max"),
    avgScoreMin: num("avg_score_min"),
    avgScoreMax: num("avg_score_max"),
    totalTokensMin: num("total_tokens_min"),
    totalTokensMax: num("total_tokens_max"),
    runtimeTotalMin: num("runtime_total_min"),
    runtimeTotalMax: num("runtime_total_max"),
    totalTrialsMin: num("total_trials_min"),
    passCountMin: num("pass_count_min"),
    partialCountMin: num("partial_count_min"),
    failCountMin: num("fail_count_min"),
    harnessCountMin: num("harness_count_min"),
    sort: sp.get("sort"),
  };
}

import type { TaskBrowseFacets } from "@/lib/types";

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
  createdAfter: string | null; // ISO datetime
  createdBefore: string | null; // ISO datetime
  minAttempts: number | null;
  minTokens: number | null;
  maxTokens: number | null;
  minSteps: number | null;
  maxSteps: number | null;
  rewardMin: number | null;
  rewardMax: number | null;
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
  minAttempts: null,
  minTokens: null,
  maxTokens: null,
  minSteps: null,
  maxSteps: null,
  rewardMin: null,
  rewardMax: null,
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
export const TRIAL_STATUS_OPTIONS: Option[] = [
  { value: "success", label: "Success" },
  { value: "failed", label: "Failed" },
  { value: "running", label: "Running" },
  { value: "queued", label: "Queued" },
  { value: "retrying", label: "Retrying" },
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
  | "agentmodel";

export interface FilterDef {
  key: string;
  label: string;
  group: "Task" | "Trial";
  control: ControlKind;
  options?: Option[];
  facet?: keyof TaskBrowseFacets;
  pinned?: boolean;
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
  },
  {
    key: "verdictStatuses",
    label: "Verdict",
    group: "Task",
    control: "select",
    options: VERDICT_OPTIONS,
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
  },
  { key: "hasError", label: "Has error", group: "Trial", control: "boolean" },
  {
    key: "hasLink",
    label: "Has source link",
    group: "Task",
    control: "boolean",
  },
  { key: "tokens", label: "Token size", group: "Trial", control: "numrange" },
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
      return f.createdAfter !== null || f.createdBefore !== null;
    case "minAttempts":
      return f.minAttempts !== null;
    case "tokens":
      return f.minTokens !== null || f.maxTokens !== null;
    case "steps":
      return f.minSteps !== null || f.maxSteps !== null;
    case "reward":
      return f.rewardMin !== null || f.rewardMax !== null;
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
  num("min_attempts", f.minAttempts);
  num("min_tokens", f.minTokens);
  num("max_tokens", f.maxTokens);
  num("min_steps", f.minSteps);
  num("max_steps", f.maxSteps);
  num("reward_min", f.rewardMin);
  num("reward_max", f.rewardMax);
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
  "min_attempts",
  "min_tokens",
  "max_tokens",
  "min_steps",
  "max_steps",
  "reward_min",
  "reward_max",
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
    minAttempts: num("min_attempts"),
    minTokens: num("min_tokens"),
    maxTokens: num("max_tokens"),
    minSteps: num("min_steps"),
    maxSteps: num("max_steps"),
    rewardMin: num("reward_min"),
    rewardMax: num("reward_max"),
  };
}

import type { TaskBrowseFacets } from "@/lib/types";

export interface FilterValues {
  statuses: string[];
  priorities: string[];
  verdictStatuses: string[];
  agents: string[];
  models: string[];
  providers: string[];
  environments: string[];
  trialStatuses: string[];
  origins: string[];
  analysisClassifications: string[];
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
  providers: [],
  environments: [],
  trialStatuses: [],
  origins: [],
  analysisClassifications: [],
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

// Enum-valued options are static. Values match the stored DB form (enum names
// for task status / priority / verdict / trial status; enum values for origin),
// which the backend resolves; see browse_tasks_core.
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

export const TRIAL_STATUS_OPTIONS: Option[] = [
  { value: "SUCCESS", label: "Success" },
  { value: "FAILED", label: "Failed" },
  { value: "RUNNING", label: "Running" },
  { value: "QUEUED", label: "Queued" },
  { value: "BLOCKED", label: "Blocked" },
  { value: "CANCELLED", label: "Cancelled" },
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
  | "num";

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
    key: "agents",
    label: "Agent",
    group: "Trial",
    control: "multiselect",
    facet: "agents",
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
    key: "models",
    label: "Model",
    group: "Trial",
    control: "multiselect",
    facet: "models",
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
  csv("providers", f.providers);
  csv("environments", f.environments);
  csv("trial_statuses", f.trialStatuses);
  csv("origins", f.origins);
  csv("analysis_classifications", f.analysisClassifications);
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

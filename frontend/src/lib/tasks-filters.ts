import type { TaskBrowseFacets } from "@/lib/types";

// Rolling "Created" presets. Stored in the URL as a token (e.g. created_within=24h)
// and resolved to (now - window) at query time, so the window is always relative
// to when the page is loaded/refreshed — not frozen at the moment it was clicked.
export const PRESET_MS = {
  "24h": 24 * 60 * 60 * 1000,
  "7d": 7 * 24 * 60 * 60 * 1000,
  "30d": 30 * 24 * 60 * 60 * 1000,
  "90d": 90 * 24 * 60 * 60 * 1000,
} as const;

export type CreatedPreset = keyof typeof PRESET_MS;

// A "Compare A vs B" condition object (mirrors the global compare params); can
// appear inside an OR-group under the reserved ``compare`` key.
export type CompareCond = {
  compare_by?: string;
  compare_a?: string;
  compare_b?: string;
  compare_metric?: string;
  compare_agg?: string;
  compare_margin?: number;
  compare_margin_unit?: string;
};

// A single OR-group condition set: backend field keys → value (same keys as the
// flat browse params), plus an optional nested ``compare`` condition.
export type OrGroup = Record<string, string[] | number | boolean | CompareCond>;

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
  experimentIds: string[];
  tagsAll: string[];
  tagsAny: string[];
  tagsNone: string[];
  hasLink: boolean | null;
  hasError: boolean | null;
  hasTrajectory: boolean | null;
  trialIsProbe: boolean | null;
  createdAfter: string | null; // ISO datetime (custom From)
  createdBefore: string | null; // ISO datetime (custom To)
  createdWithin: CreatedPreset | null; // rolling preset (24h/7d/30d/90d)
  trialFinishedAfter: string | null;
  trialFinishedBefore: string | null;
  trialFinishedWithin: CreatedPreset | null;
  minAttempts: number | null;
  minTokens: number | null;
  maxTokens: number | null;
  minSteps: number | null;
  maxSteps: number | null;
  minDurationSeconds: number | null;
  maxDurationSeconds: number | null;
  minToolCalls: number | null;
  maxToolCalls: number | null;
  toolNames: string[];
  trialMetricMatch: "any" | "all";
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
  runtimeAvgMin: number | null; // seconds, per trial
  runtimeAvgMax: number | null; // seconds, per trial
  totalTrialsMin: number | null;
  completedTrialsMin: number | null;
  failedTrialsMin: number | null;
  passCountMin: number | null;
  partialCountMin: number | null;
  failCountMin: number | null;
  harnessCountMin: number | null;
  sort: string | null; // aggregate sort token (e.g. "avg_score_desc")
  // Phase 2.1 agent/model comparison ("A beats B" on a metric).
  compareBy: string | null; // "agent" | "model"
  compareA: string | null;
  compareB: string | null;
  compareMetric: string | null; // "reward" | "runtime" | "tokens" | "steps"
  compareAgg: string | null; // "best" | "avg"
  compareMargin: number | null;
  compareMarginUnit: string | null; // "pct" | "abs"
  // Phase 2.3 pass-rate range filter + top performer (best agent/model per task).
  passRateMin: number | null; // percent 0-100
  passRateMax: number | null; // percent 0-100
  topBy: string | null; // "agent" | "model"
  topValue: string | null;
  topMetric: string | null;
  // Phase 2.2 "Match any of…" — OR of AND-groups, ANDed with everything above.
  orGroups: OrGroup[] | null;
}

export interface Option {
  value: string;
  label: string;
}

// Enum-valued options are static and MUST match the stored DB form, because the
// trial-status and origin columns use `values_callable` (no name coercion):
//   - task status / priority / verdict: enum NAMES (uppercase) — default SQLEnum
//   - trial status / origin: enum VALUES (lowercase) — values_callable columns
// See db/models.py and browse_tasks_core.
const STATUS_OPTIONS: Option[] = [
  { value: "COMPLETED", label: "Completed" },
  { value: "RUNNING", label: "Running" },
  { value: "PENDING", label: "Pending" },
  { value: "ANALYZING", label: "Analyzing" },
  { value: "VERDICT_PENDING", label: "Verdict pending" },
  { value: "FAILED", label: "Failed" },
  { value: "CANCELLED", label: "Cancelled" },
];

const PRIORITY_OPTIONS: Option[] = [
  { value: "HIGH", label: "High" },
  { value: "LOW", label: "Low" },
];

const VERDICT_OPTIONS: Option[] = [
  { value: "SUCCESS", label: "Pass" },
  { value: "FAILED", label: "Fail" },
  { value: "PENDING", label: "Pending" },
];

// TrialStatus = JobStatus, stored as lowercase values via values_callable.
// (BLOCKED/CANCELLED belong to WorkerJobStatus, not trials.)
// Values are the enum *names* (uppercase): TrialModel.status uses SQLEnum without
// values_callable, so the column stores SUCCESS/FAILED/… not the lowercase enum
// values. Matches the TaskStatus filter pattern; lowercase here matches no rows.
const TRIAL_STATUS_OPTIONS: Option[] = [
  { value: "SUCCESS", label: "Success" },
  { value: "FAILED", label: "Failed" },
  { value: "RUNNING", label: "Running" },
  { value: "QUEUED", label: "Queued" },
  { value: "RETRYING", label: "Retrying" },
];

const ORIGIN_OPTIONS: Option[] = [
  { value: "oddish", label: "Oddish" },
  { value: "imported", label: "Imported" },
];

type ControlKind =
  | "multiselect"
  | "select"
  | "boolean"
  | "daterange"
  | "numrange"
  | "rewardthreshold"
  | "num"
  | "tags"
  | "agentmodel"
  | "experiment"
  | "sort"
  | "compare"
  | "top"
  | "matchany"
  | "metricmatch"
  | "toolnames";

// Conditions available inside an OR-group ("Match any of…"). Each maps to the
// backend field key(s) it writes; the union of these is what a group can hold.
export interface GroupConditionDef {
  id: string;
  label: string;
  control: "multiselect" | "boolean" | "numrange" | "num" | "compare";
  options?: Option[];
  facet?: keyof TaskBrowseFacets;
  keys: string[]; // 1 key, or [minKey, maxKey] for numrange, or ["compare"]
}

// Phase 2.1 agent/model comparison option lists.
export const COMPARE_SUBJECT_OPTIONS: Option[] = [
  { value: "agent", label: "Agent" },
  { value: "model", label: "Model" },
];
export const COMPARE_METRIC_OPTIONS: Option[] = [
  { value: "reward", label: "Reward (pts)" },
  { value: "runtime", label: "Run time (seconds)" },
  { value: "tokens", label: "Tokens" },
  { value: "steps", label: "Steps" },
  { value: "pass_rate", label: "Pass rate (%)" },
];
// Plain metric word for the "reads as" summary (keeps the unit out of the prose,
// which already carries it in the margin text).
export const COMPARE_METRIC_WORD: Record<string, string> = {
  reward: "reward",
  runtime: "run time",
  tokens: "tokens",
  steps: "steps",
  pass_rate: "pass rate",
};
// Unit shown on the margin field when the "abs" mode is selected.
export const COMPARE_METRIC_UNIT: Record<string, string> = {
  reward: "pts",
  runtime: "seconds",
  tokens: "tokens",
  steps: "steps",
  pass_rate: "%",
};
export const COMPARE_AGG_OPTIONS: Option[] = [
  { value: "best", label: "Best" },
  { value: "avg", label: "Average" },
  { value: "median", label: "Median" },
];
export const COMPARE_MARGIN_UNIT_OPTIONS: Option[] = [
  { value: "pct", label: "%" },
  { value: "abs", label: "abs" },
];

// Aggregate sort tokens (kept in sync with backend ``_AGGREGATE_SORTS``).
export const SORT_OPTIONS: Option[] = [
  { value: "cost_desc", label: "Cost (high → low)" },
  { value: "avg_score_desc", label: "Avg score (high → low)" },
  { value: "avg_score_asc", label: "Avg score (low → high)" },
  { value: "total_tokens_desc", label: "Total tokens (high → low)" },
  { value: "total_tokens_asc", label: "Total tokens (low → high)" },
  { value: "runtime_total_desc", label: "Run time (long → short)" },
  { value: "runtime_total_asc", label: "Run time (short → long)" },
  { value: "runtime_avg_desc", label: "Avg run time (long → short)" },
  { value: "runtime_avg_asc", label: "Avg run time (short → long)" },
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
    key: "models",
    label: "Model",
    group: "Trial",
    control: "multiselect",
    facet: "models",
  },
  {
    key: "trialFinished",
    label: "Trial finished",
    group: "Trial",
    control: "daterange",
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
  // Options come from the async /api/tasks/browse/experiment-options endpoint
  // (an org can hold 100k+ experiments), NOT from the facets payload — so no
  // `facet` key here.
  {
    key: "experiments",
    label: "Experiment",
    group: "Task",
    control: "experiment",
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
    key: "trajectoryDuration",
    label: "Trajectory time (s)",
    group: "Trial",
    control: "numrange",
  },
  {
    key: "toolCalls",
    label: "Tool calls",
    group: "Trial",
    control: "numrange",
  },
  {
    key: "toolNames",
    label: "Tool names",
    group: "Trial",
    control: "toolnames",
  },
  {
    key: "trialMetricMatch",
    label: "Match metrics across",
    group: "Trial",
    control: "metricmatch",
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
    key: "runtimeAvg",
    label: "Run time (s, avg/trial)",
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
    key: "completedTrials",
    label: "Completed trials ≥",
    group: "Task",
    control: "num",
  },
  {
    key: "failedTrials",
    label: "Failed trials ≥",
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
  {
    key: "agentCompare",
    label: "Compare A vs B",
    group: "Trial",
    control: "compare",
  },
  {
    key: "topPerformer",
    label: "Top performer",
    group: "Trial",
    control: "top",
  },
  {
    key: "passRate",
    label: "Pass rate %",
    group: "Task",
    control: "numrange",
  },
  {
    key: "matchAny",
    label: "Match any of…",
    group: "Task",
    control: "matchany",
  },
];

// Conditions offered inside an OR-group. Keys match the backend flat params.
export const CONDITION_DEFS: GroupConditionDef[] = [
  {
    id: "statuses",
    label: "Status",
    control: "multiselect",
    options: STATUS_OPTIONS,
    keys: ["statuses"],
  },
  {
    id: "agents",
    label: "Agent",
    control: "multiselect",
    facet: "agents",
    keys: ["agents"],
  },
  {
    id: "models",
    label: "Model",
    control: "multiselect",
    facet: "models",
    keys: ["models"],
  },
  {
    id: "providers",
    label: "Provider",
    control: "multiselect",
    facet: "providers",
    keys: ["providers"],
  },
  {
    id: "environments",
    label: "Environment",
    control: "multiselect",
    facet: "environments",
    keys: ["environments"],
  },
  {
    id: "trialStatuses",
    label: "Trial status",
    control: "multiselect",
    options: TRIAL_STATUS_OPTIONS,
    keys: ["trial_statuses"],
  },
  {
    id: "origins",
    label: "Origin",
    control: "multiselect",
    options: ORIGIN_OPTIONS,
    keys: ["origins"],
  },
  {
    id: "analysisClassifications",
    label: "Analysis result",
    control: "multiselect",
    facet: "analysis_classifications",
    keys: ["analysis_classifications"],
  },
  {
    id: "hasError",
    label: "Has error",
    control: "boolean",
    keys: ["has_error"],
  },
  {
    id: "reward",
    label: "Reward (0–1)",
    control: "numrange",
    keys: ["reward_min", "reward_max"],
  },
  {
    id: "tokens",
    label: "Token size",
    control: "numrange",
    keys: ["min_tokens", "max_tokens"],
  },
  {
    id: "steps",
    label: "Trajectory length",
    control: "numrange",
    keys: ["min_steps", "max_steps"],
  },
  {
    id: "avgScore",
    label: "Avg score %",
    control: "numrange",
    keys: ["avg_score_min", "avg_score_max"],
  },
  {
    id: "totalTokens",
    label: "Total tokens",
    control: "numrange",
    keys: ["total_tokens_min", "total_tokens_max"],
  },
  {
    id: "runtime",
    label: "Run time (s)",
    control: "numrange",
    keys: ["runtime_total_min", "runtime_total_max"],
  },
  {
    id: "passRate",
    label: "Pass rate %",
    control: "numrange",
    keys: ["pass_rate_min", "pass_rate_max"],
  },
  {
    id: "passCount",
    label: "Pass count ≥",
    control: "num",
    keys: ["pass_count_min"],
  },
  {
    id: "failCount",
    label: "Fail count ≥",
    control: "num",
    keys: ["fail_count_min"],
  },
  {
    id: "harnessCount",
    label: "Harness count ≥",
    control: "num",
    keys: ["harness_count_min"],
  },
  {
    id: "totalTrials",
    label: "Total trials ≥",
    control: "num",
    keys: ["total_trials_min"],
  },
  {
    id: "compare",
    label: "Compare A vs B",
    control: "compare",
    keys: ["compare"],
  },
];

// Drop empty conditions (empty arrays / blank) and empty groups, so serialization
// and the active check reflect only real conditions.
export function cleanOrGroups(groups: OrGroup[]): OrGroup[] {
  const out: OrGroup[] = [];
  for (const group of groups) {
    const cleaned: OrGroup = {};
    for (const [key, value] of Object.entries(group)) {
      if (key.startsWith("_")) continue; // UI meta (e.g. shown-condition ids)
      if (Array.isArray(value)) {
        if (value.length) cleaned[key] = value;
      } else if (typeof value === "number") {
        if (!Number.isNaN(value)) cleaned[key] = value;
      } else if (typeof value === "boolean") {
        cleaned[key] = value;
      } else if (value && typeof value === "object") {
        // The nested "compare" condition — keep only a complete, distinct pair.
        const c = value as CompareCond;
        if (c.compare_a && c.compare_b && c.compare_a !== c.compare_b) {
          cleaned[key] = value;
        }
      }
    }
    if (Object.keys(cleaned).length) out.push(cleaned);
  }
  return out;
}

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
    case "experiments":
      return f.experimentIds.length > 0;
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
    case "trialFinished":
      return (
        f.trialFinishedAfter !== null ||
        f.trialFinishedBefore !== null ||
        f.trialFinishedWithin !== null
      );
    case "minAttempts":
      return f.minAttempts !== null;
    case "tokens":
      return f.minTokens !== null || f.maxTokens !== null;
    case "steps":
      return f.minSteps !== null || f.maxSteps !== null;
    case "trajectoryDuration":
      return f.minDurationSeconds !== null || f.maxDurationSeconds !== null;
    case "toolCalls":
      return f.minToolCalls !== null || f.maxToolCalls !== null;
    case "toolNames":
      return f.toolNames.length > 0;
    case "trialMetricMatch":
      return f.trialMetricMatch === "all";
    case "reward":
      return f.rewardMin !== null || f.rewardMax !== null;
    case "avgScore":
      return f.avgScoreMin !== null || f.avgScoreMax !== null;
    case "totalTokens":
      return f.totalTokensMin !== null || f.totalTokensMax !== null;
    case "runtime":
      return f.runtimeTotalMin !== null || f.runtimeTotalMax !== null;
    case "runtimeAvg":
      return f.runtimeAvgMin !== null || f.runtimeAvgMax !== null;
    case "totalTrials":
      return f.totalTrialsMin !== null;
    case "completedTrials":
      return f.completedTrialsMin !== null;
    case "failedTrials":
      return f.failedTrialsMin !== null;
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
    case "agentCompare":
      return f.compareA !== null && f.compareB !== null;
    case "passRate":
      return f.passRateMin !== null || f.passRateMax !== null;
    case "topPerformer":
      return f.topValue !== null;
    case "matchAny":
      return f.orGroups !== null && cleanOrGroups(f.orGroups).length > 0;
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
  csv("experiment_ids", f.experimentIds);
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
  if (f.trialFinishedAfter)
    out.push(["trial_finished_after", f.trialFinishedAfter]);
  if (f.trialFinishedBefore)
    out.push(["trial_finished_before", f.trialFinishedBefore]);
  if (f.trialFinishedWithin)
    out.push(["trial_finished_within", f.trialFinishedWithin]);
  num("min_attempts", f.minAttempts);
  num("min_tokens", f.minTokens);
  num("max_tokens", f.maxTokens);
  num("min_steps", f.minSteps);
  num("max_steps", f.maxSteps);
  num("min_duration_seconds", f.minDurationSeconds);
  num("max_duration_seconds", f.maxDurationSeconds);
  num("min_tool_calls", f.minToolCalls);
  num("max_tool_calls", f.maxToolCalls);
  csv("tool_names", f.toolNames);
  if (f.trialMetricMatch === "all") out.push(["trial_metric_match", "all"]);
  num("reward_min", f.rewardMin);
  num("reward_max", f.rewardMax);
  num("avg_score_min", f.avgScoreMin);
  num("avg_score_max", f.avgScoreMax);
  num("total_tokens_min", f.totalTokensMin);
  num("total_tokens_max", f.totalTokensMax);
  num("runtime_total_min", f.runtimeTotalMin);
  num("runtime_total_max", f.runtimeTotalMax);
  num("runtime_avg_min", f.runtimeAvgMin);
  num("runtime_avg_max", f.runtimeAvgMax);
  num("total_trials_min", f.totalTrialsMin);
  num("completed_trials_min", f.completedTrialsMin);
  num("failed_trials_min", f.failedTrialsMin);
  num("pass_count_min", f.passCountMin);
  num("partial_count_min", f.partialCountMin);
  num("fail_count_min", f.failCountMin);
  num("harness_count_min", f.harnessCountMin);
  if (f.sort) out.push(["sort", f.sort]);
  // Agent/model comparison — only serialize a complete, distinct pair.
  if (f.compareA && f.compareB && f.compareA !== f.compareB) {
    out.push(["compare_by", f.compareBy || "agent"]);
    out.push(["compare_a", f.compareA]);
    out.push(["compare_b", f.compareB]);
    out.push(["compare_metric", f.compareMetric || "reward"]);
    out.push(["compare_agg", f.compareAgg || "best"]);
    if (f.compareMargin != null && !Number.isNaN(f.compareMargin)) {
      out.push(["compare_margin", String(f.compareMargin)]);
      out.push(["compare_margin_unit", f.compareMarginUnit || "pct"]);
    }
  }
  num("pass_rate_min", f.passRateMin);
  num("pass_rate_max", f.passRateMax);
  // Top performer — needs a subject value; metric defaults to reward.
  if (f.topValue) {
    out.push(["top_by", f.topBy || "agent"]);
    out.push(["top_value", f.topValue]);
    out.push(["top_metric", f.topMetric || "reward"]);
  }
  // OR-groups — one compact JSON param; only serialize non-empty groups.
  if (f.orGroups) {
    const cleaned = cleanOrGroups(f.orGroups);
    if (cleaned.length) out.push(["or_groups", JSON.stringify(cleaned)]);
  }
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
  "experiment_ids",
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
  "trial_finished_after",
  "trial_finished_before",
  "trial_finished_within",
  "min_attempts",
  "min_tokens",
  "max_tokens",
  "min_steps",
  "max_steps",
  "min_duration_seconds",
  "max_duration_seconds",
  "min_tool_calls",
  "max_tool_calls",
  "tool_names",
  "trial_metric_match",
  "reward_min",
  "reward_max",
  "avg_score_min",
  "avg_score_max",
  "total_tokens_min",
  "total_tokens_max",
  "runtime_total_min",
  "runtime_total_max",
  "runtime_avg_min",
  "runtime_avg_max",
  "total_trials_min",
  "completed_trials_min",
  "failed_trials_min",
  "pass_count_min",
  "partial_count_min",
  "fail_count_min",
  "harness_count_min",
  "sort",
  "compare_by",
  "compare_a",
  "compare_b",
  "compare_metric",
  "compare_agg",
  "compare_margin",
  "compare_margin_unit",
  "pass_rate_min",
  "pass_rate_max",
  "top_by",
  "top_value",
  "top_metric",
  "or_groups",
] as const;

// Backend filter params that have no sidebar control yet but are still valid on
// /tasks/browse (set via deep links or saved filters). The server results
// loader forwards these in addition to FILTER_PARAM_KEYS so they aren't
// silently dropped. They're intentionally NOT in FILTER_PARAM_KEYS so the
// sidebar's clear-on-change loop doesn't wipe deep-linked values.
// (`experiment_ids` graduated to FILTER_PARAM_KEYS with the Experiment
// filter control; it now round-trips through searchParamsToFilters.)
const EXTRA_BROWSE_PARAM_KEYS = [
  "run_analysis",
  "run_probe",
  "harbor_shas",
  "harbor_stages",
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
    experimentIds: csv("experiment_ids"),
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
    trialFinishedAfter: sp.get("trial_finished_after"),
    trialFinishedBefore: sp.get("trial_finished_before"),
    trialFinishedWithin: ((): CreatedPreset | null => {
      const v = sp.get("trial_finished_within");
      return v && v in PRESET_MS ? (v as CreatedPreset) : null;
    })(),
    minAttempts: num("min_attempts"),
    minTokens: num("min_tokens"),
    maxTokens: num("max_tokens"),
    minSteps: num("min_steps"),
    maxSteps: num("max_steps"),
    minDurationSeconds: num("min_duration_seconds"),
    maxDurationSeconds: num("max_duration_seconds"),
    minToolCalls: num("min_tool_calls"),
    maxToolCalls: num("max_tool_calls"),
    toolNames: csv("tool_names"),
    trialMetricMatch: sp.get("trial_metric_match") === "all" ? "all" : "any",
    rewardMin: num("reward_min"),
    rewardMax: num("reward_max"),
    avgScoreMin: num("avg_score_min"),
    avgScoreMax: num("avg_score_max"),
    totalTokensMin: num("total_tokens_min"),
    totalTokensMax: num("total_tokens_max"),
    runtimeTotalMin: num("runtime_total_min"),
    runtimeTotalMax: num("runtime_total_max"),
    runtimeAvgMin: num("runtime_avg_min"),
    runtimeAvgMax: num("runtime_avg_max"),
    totalTrialsMin: num("total_trials_min"),
    completedTrialsMin: num("completed_trials_min"),
    failedTrialsMin: num("failed_trials_min"),
    passCountMin: num("pass_count_min"),
    partialCountMin: num("partial_count_min"),
    failCountMin: num("fail_count_min"),
    harnessCountMin: num("harness_count_min"),
    sort: sp.get("sort"),
    compareBy: sp.get("compare_by"),
    compareA: sp.get("compare_a"),
    compareB: sp.get("compare_b"),
    compareMetric: sp.get("compare_metric"),
    compareAgg: sp.get("compare_agg"),
    compareMargin: num("compare_margin"),
    compareMarginUnit: sp.get("compare_margin_unit"),
    passRateMin: num("pass_rate_min"),
    passRateMax: num("pass_rate_max"),
    topBy: sp.get("top_by"),
    topValue: sp.get("top_value"),
    topMetric: sp.get("top_metric"),
    orGroups: ((): OrGroup[] | null => {
      const raw = sp.get("or_groups");
      if (!raw) return null;
      try {
        const parsed = JSON.parse(raw);
        if (!Array.isArray(parsed)) return null;
        const groups = parsed.filter(
          (g): g is OrGroup => typeof g === "object" && g !== null
        );
        return groups.length ? groups : null;
      } catch {
        return null;
      }
    })(),
  };
}

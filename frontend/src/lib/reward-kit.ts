/**
 * RewardKit reward structures: the named-rewards map, the per-criterion
 * breakdown, and the task-side reward design.
 *
 * Two data sources feed these types:
 *
 * 1. Trial output — the worker embeds `_rewards` (every named score from
 *    `verifier/reward.json`) and `_reward_details` (a bounded summary of
 *    `verifier/reward-details.json`) in `trials.result`. The full details
 *    document stays in object storage and is lazy-loaded through the scoped
 *    trial files API when the embedded copy is missing (imports, historical
 *    trials) or truncated.
 *
 * 2. Task design — `tests/reward.toml` and the judge TOMLs are declarative,
 *    so the reward program (dimensions, criteria, weights, aggregation) can
 *    be reconstructed from the task's files without executing anything.
 *    Python criteria are enumerated best-effort with a static scan.
 *
 * Parsing here is forgiving to match the backend extractors: anything
 * malformed yields null rather than throwing.
 */

import { parse as parseToml } from "smol-toml";

// ---------------------------------------------------------------------------
// Shared helpers

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function nonEmptyString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

// ---------------------------------------------------------------------------
// Trial output: named rewards map (`result._rewards` / reward.json)

export type RewardsMap = Record<string, number>;

export function parseRewardsMap(value: unknown): RewardsMap | null {
  if (!isRecord(value)) return null;
  const map: RewardsMap = {};
  for (const [key, raw] of Object.entries(value)) {
    const num = finiteNumber(raw);
    if (num !== null) map[key] = num;
  }
  return Object.keys(map).length > 0 ? map : null;
}

export function embeddedRewardsMap(
  result: Record<string, unknown> | null | undefined
): RewardsMap | null {
  return parseRewardsMap(result?._rewards);
}

/** Named rewards that are aggregates vs. dimensions, for display grouping. */
export function splitRewardsMap(
  rewards: RewardsMap,
  dimensionNames: Set<string>
): { dimensions: [string, number][]; aggregates: [string, number][] } {
  const dimensions: [string, number][] = [];
  const aggregates: [string, number][] = [];
  for (const entry of Object.entries(rewards)) {
    (dimensionNames.has(entry[0]) ? dimensions : aggregates).push(entry);
  }
  return { dimensions, aggregates };
}

// ---------------------------------------------------------------------------
// Trial output: per-criterion breakdown
// (`result._reward_details` / reward-details.json)

export type RewardKind = "programmatic" | "llm" | "agent";

export interface RewardCriterionResult {
  name: string;
  /** Normalized score in [0, 1]. */
  value: number;
  weight: number;
  description?: string;
  reasoning?: string;
  error?: string;
  /** Pre-normalization judge/check output ("yes", 4, true, …). */
  raw?: unknown;
  negate?: boolean;
  optional?: boolean;
}

export interface RewardDimensionResult {
  name: string;
  score: number;
  kind: RewardKind | null;
  /** Judge model or agent id, when the dimension is judge-based. */
  judge?: string;
  /** Exact files the judge read, when the full document is loaded. */
  judgeFiles?: string[];
  /** Trajectory file attached to the judge, when the full document is loaded. */
  judgeTrajectory?: string;
  criteria: RewardCriterionResult[];
  /** Total criteria in the source document when the list here is capped. */
  criteriaTotal?: number;
  warningsCount?: number;
  warnings?: string[];
}

export interface RewardBreakdown {
  dimensions: RewardDimensionResult[];
  /** Path of the full details document inside the trial artifacts. */
  detailsPath: string | null;
  /** True when the embedded summary dropped text/criteria for size. */
  truncated: boolean;
  /** Where this breakdown came from. */
  source: "embedded" | "artifact";
}

function parseKind(value: unknown): RewardKind | null {
  return value === "programmatic" || value === "llm" || value === "agent"
    ? value
    : null;
}

function parseCriterion(value: unknown): RewardCriterionResult | null {
  if (!isRecord(value)) return null;
  const name = nonEmptyString(value.name);
  const score = finiteNumber(value.value);
  if (name === null || score === null) return null;
  const criterion: RewardCriterionResult = {
    name,
    value: score,
    weight: finiteNumber(value.weight) ?? 1,
  };
  const description = nonEmptyString(value.description);
  if (description) criterion.description = description;
  const reasoning = nonEmptyString(value.reasoning);
  if (reasoning) criterion.reasoning = reasoning;
  const error = nonEmptyString(value.error);
  if (error) criterion.error = error;
  if ("raw" in value && value.raw !== undefined) criterion.raw = value.raw;
  if (value.negate === true) criterion.negate = true;
  if (value.optional === true) criterion.optional = true;
  return criterion;
}

function parseDimensionEntry(
  name: string,
  value: unknown
): RewardDimensionResult | null {
  if (!isRecord(value)) return null;
  const score = finiteNumber(value.score);
  if (score === null) return null;
  const dimension: RewardDimensionResult = {
    name,
    score,
    kind: parseKind(value.kind),
    criteria: [],
  };

  // Embedded summaries carry `judge` as a string; the raw document carries
  // the full judge config object.
  const judgeString = nonEmptyString(value.judge);
  if (judgeString) {
    dimension.judge = judgeString;
  } else if (isRecord(value.judge)) {
    const judge = value.judge;
    const model = nonEmptyString(judge.model) ?? nonEmptyString(judge.agent);
    if (model) dimension.judge = model;
    if (Array.isArray(judge.files)) {
      const files = judge.files.filter(
        (f): f is string => typeof f === "string" && f.length > 0
      );
      if (files.length > 0) dimension.judgeFiles = files;
    }
    const trajectory = nonEmptyString(judge.atif_trajectory);
    if (trajectory) dimension.judgeTrajectory = trajectory;
  }

  if (Array.isArray(value.criteria)) {
    for (const entry of value.criteria) {
      const criterion = parseCriterion(entry);
      if (criterion) dimension.criteria.push(criterion);
    }
  }
  const criteriaTotal = finiteNumber(value.criteria_total);
  if (criteriaTotal !== null && criteriaTotal > dimension.criteria.length) {
    dimension.criteriaTotal = criteriaTotal;
  }
  if (Array.isArray(value.warnings)) {
    const warnings = value.warnings.filter(
      (w): w is string => typeof w === "string"
    );
    dimension.warningsCount = value.warnings.length;
    if (warnings.length > 0) dimension.warnings = warnings;
  } else {
    const count = finiteNumber(value.warnings);
    if (count !== null && count > 0) dimension.warningsCount = count;
  }
  return dimension;
}

/** Parse the embedded `_reward_details` summary the worker persists. */
export function embeddedRewardBreakdown(
  result: Record<string, unknown> | null | undefined
): RewardBreakdown | null {
  const value = result?._reward_details;
  if (!isRecord(value) || value.format !== "rewardkit") return null;
  if (!Array.isArray(value.dimensions)) return null;
  const dimensions: RewardDimensionResult[] = [];
  for (const entry of value.dimensions) {
    if (!isRecord(entry)) continue;
    const name = nonEmptyString(entry.name);
    if (!name) continue;
    const dimension = parseDimensionEntry(name, entry);
    if (dimension) dimensions.push(dimension);
  }
  if (dimensions.length === 0) return null;
  return {
    dimensions,
    detailsPath: nonEmptyString(value.details_path),
    truncated: value.truncated === true,
    source: "embedded",
  };
}

/** Parse a raw reward-details.json document (dimension name → entry|entries). */
export function parseRewardDetailsDocument(
  value: unknown
): RewardBreakdown | null {
  if (!isRecord(value)) return null;
  const dimensions: RewardDimensionResult[] = [];
  for (const [name, raw] of Object.entries(value)) {
    const entries = Array.isArray(raw) ? raw : [raw];
    for (const entry of entries) {
      const dimension = parseDimensionEntry(name, entry);
      if (dimension === null) return null;
      dimensions.push(dimension);
    }
  }
  if (dimensions.length === 0) return null;
  return {
    dimensions,
    detailsPath: null,
    truncated: false,
    source: "artifact",
  };
}

// ---------------------------------------------------------------------------
// Aggregation (mirrors rewardkit.reward.aggregate_scores)

export type RewardAggregation =
  | "weighted_mean"
  | "all_pass"
  | "any_pass"
  | "threshold"
  | "required_pass";

export interface ScoreEntry {
  value: number;
  weight: number;
  optional?: boolean;
}

export function aggregateScores(
  scores: ScoreEntry[],
  aggregation: string,
  threshold = 0.5
): number {
  if (scores.length === 0) return 0;
  if (aggregation === "all_pass") {
    return scores.every((s) => s.value > 0) ? 1 : 0;
  }
  if (aggregation === "any_pass") {
    return scores.some((s) => s.value > 0) ? 1 : 0;
  }
  if (aggregation === "required_pass") {
    const required = scores.filter((s) => !s.optional);
    if (required.length === 0) return 0;
    return required.every((s) => s.value > 0) ? 1 : 0;
  }
  const totalWeight = scores.reduce((sum, s) => sum + s.weight, 0);
  const mean =
    totalWeight === 0
      ? 0
      : scores.reduce((sum, s) => sum + s.value * s.weight, 0) / totalWeight;
  if (aggregation === "threshold") return mean >= threshold ? 1 : 0;
  return mean;
}

// ---------------------------------------------------------------------------
// Task design: the reward program reconstructed from tests/

export interface RewardDesignCriterion {
  name: string;
  description: string | null;
  /** binary | likert | numeric for judge criteria; builtin/custom for python. */
  type: string | null;
  weight: number;
  optional: boolean;
  /** File the criterion is defined in, repo-relative. */
  source: string;
}

export interface RewardDesignDimension {
  name: string;
  kind: RewardKind;
  aggregation: RewardAggregation;
  threshold: number;
  /** Dimension weight: judge weight, or the sum of its criteria weights. */
  weight: number;
  judgeModel?: string;
  judgeFiles?: string[];
  judgeTrajectory?: string;
  criteria: RewardDesignCriterion[];
  /**
   * False when a python criteria file was found but its criteria could not
   * be statically enumerated (dynamic registration); the criteria list is
   * then a lower bound.
   */
  criteriaComplete: boolean;
  source: string;
}

export interface RewardDesignAggregate {
  name: string;
  aggregation: RewardAggregation;
  threshold: number;
}

export interface RewardDesign {
  dimensions: RewardDesignDimension[];
  aggregates: RewardDesignAggregate[];
  /** Files the design was reconstructed from. */
  sources: string[];
}

export interface TaskFileContent {
  path: string;
  content: string;
}

const AGGREGATIONS = new Set<string>([
  "weighted_mean",
  "all_pass",
  "any_pass",
  "threshold",
  "required_pass",
]);

function parseAggregation(value: unknown): RewardAggregation {
  return AGGREGATIONS.has(value as string)
    ? (value as RewardAggregation)
    : "weighted_mean";
}

/** `tests/exact/check.py` → `exact`; `tests/check.py` → `reward`. */
function dimensionNameFor(path: string, testsPrefix: string): string {
  const relative = path.slice(testsPrefix.length);
  const slash = relative.indexOf("/");
  return slash === -1 ? "reward" : relative.slice(0, slash);
}

function safeParseToml(content: string): Record<string, unknown> | null {
  try {
    const parsed = parseToml(content);
    return isRecord(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

interface JudgeTomlDesign {
  judgeModel?: string;
  judgeFiles?: string[];
  judgeTrajectory?: string;
  weight: number;
  kind: RewardKind;
  aggregation: RewardAggregation;
  threshold: number;
  criteria: RewardDesignCriterion[];
}

function parseJudgeToml(
  content: string,
  source: string
): JudgeTomlDesign | null {
  const doc = safeParseToml(content);
  if (!doc || !isRecord(doc.judge)) return null;
  const judge = doc.judge;
  const judgeId =
    nonEmptyString(judge.judge) ??
    nonEmptyString(judge.model) ??
    nonEmptyString(judge.agent);

  const criteria: RewardDesignCriterion[] = [];
  if (Array.isArray(doc.criterion)) {
    for (const [index, entry] of doc.criterion.entries()) {
      if (!isRecord(entry)) continue;
      const description = nonEmptyString(entry.description)?.trim() ?? null;
      criteria.push({
        name: nonEmptyString(entry.name) ?? `criterion ${index + 1}`,
        description,
        type: nonEmptyString(entry.type) ?? "binary",
        weight: finiteNumber(entry.weight) ?? 1,
        optional: entry.optional === true,
        source,
      });
    }
  }

  const scoring = isRecord(doc.scoring) ? doc.scoring : {};
  const files = Array.isArray(judge.files)
    ? judge.files.filter(
        (f): f is string => typeof f === "string" && f.length > 0
      )
    : [];
  // Agent judges name an agent (e.g. "claude-code"); LLM judges name a
  // provider-prefixed model. RewardKit routes on the same heuristic.
  const kind: RewardKind =
    nonEmptyString(judge.agent) !== null ||
    (judgeId !== null && !judgeId.includes("/"))
      ? "agent"
      : "llm";
  return {
    judgeModel: judgeId ?? undefined,
    judgeFiles: files.length > 0 ? files : undefined,
    judgeTrajectory: nonEmptyString(judge["atif-trajectory"]) ?? undefined,
    weight: finiteNumber(judge.weight) ?? 1,
    kind,
    aggregation: parseAggregation(scoring.aggregation),
    threshold: finiteNumber(scoring.threshold) ?? 0.5,
    criteria,
  };
}

/**
 * Best-effort static enumeration of python criteria: `@criterion`-decorated
 * functions and `rk.<builtin>(...)` registrations. Dynamic registration
 * (loops, computed args) cannot be enumerated statically; the flag on the
 * dimension marks the list as a lower bound in that case.
 */
function parsePythonCriteria(
  content: string,
  source: string
): { criteria: RewardDesignCriterion[]; complete: boolean } {
  const criteria: RewardDesignCriterion[] = [];

  // @criterion / @criterion(description="...", weight=2) decorated defs.
  const decorated = content.matchAll(
    /@criterion(?:\(([^)]*)\))?\s*\r?\ndef\s+(\w+)\s*\(/g
  );
  for (const match of decorated) {
    const args = match[1] ?? "";
    const descriptionMatch =
      /description\s*=\s*(?:"""([\s\S]*?)"""|"([^"]*)"|'([^']*)')/.exec(args);
    const description = descriptionMatch
      ? (descriptionMatch[1] ?? descriptionMatch[2] ?? descriptionMatch[3])
      : null;
    const weight = /weight\s*=\s*([\d.]+)/.exec(args);
    criteria.push({
      name: match[2],
      description,
      type: "custom",
      weight: weight ? Number.parseFloat(weight[1]) : 1,
      optional: /optional\s*=\s*True/.test(args),
      source,
    });
  }

  // rk.file_exists("output.txt", weight=2) style builtin registrations.
  const builtins = content.matchAll(/\brk\.(\w+)\(([^)]*)\)/g);
  for (const match of builtins) {
    const args = match[2];
    const firstArg = /^\s*(?:"([^"]*)"|'([^']*)')/.exec(args);
    const weight = /weight\s*=\s*([\d.]+)/.exec(args);
    criteria.push({
      name: match[1],
      description: firstArg ? (firstArg[1] ?? firstArg[2]) : null,
      type: "builtin",
      weight: weight ? Number.parseFloat(weight[1]) : 1,
      optional: false,
      source,
    });
  }

  // Registration inside loops or via variables → the static list is partial.
  const complete =
    !/for\s+\w+\s+in\s+[\s\S]{0,200}?(?:@criterion|rk\.\w+\()/.test(content);
  return { criteria, complete };
}

/**
 * Reconstruct the reward program from a task's `tests/` files.
 *
 * Returns null when the task does not look RewardKit-based (no reward.toml,
 * no judge TOML, and no rewardkit reference in test.sh).
 */
export function buildRewardDesign(
  files: TaskFileContent[],
  testsPrefix = "tests/"
): RewardDesign | null {
  const inTests = files.filter((f) => f.path.startsWith(testsPrefix));
  if (inTests.length === 0) return null;

  const testSh = inTests.find((f) => f.path === `${testsPrefix}test.sh`);
  const rewardToml = inTests.find(
    (f) => f.path === `${testsPrefix}reward.toml`
  );
  const isRewardKit =
    rewardToml !== undefined ||
    (testSh !== undefined && /rewardkit/i.test(testSh.content));
  if (!isRewardKit) return null;

  const sources: string[] = [];
  const byDimension = new Map<string, RewardDesignDimension>();

  const ensureDimension = (
    name: string,
    source: string
  ): RewardDesignDimension => {
    let dimension = byDimension.get(name);
    if (!dimension) {
      dimension = {
        name,
        kind: "programmatic",
        aggregation: "weighted_mean",
        threshold: 0.5,
        weight: 0,
        criteria: [],
        criteriaComplete: true,
        source,
      };
      byDimension.set(name, dimension);
    }
    return dimension;
  };

  for (const file of inTests) {
    const base = file.path.slice(file.path.lastIndexOf("/") + 1);
    if (file.path === `${testsPrefix}reward.toml`) continue;
    if (base.endsWith(".toml")) {
      const judge = parseJudgeToml(file.content, file.path);
      if (!judge) continue;
      sources.push(file.path);
      const name = dimensionNameFor(file.path, testsPrefix);
      const dimension = ensureDimension(name, file.path);
      dimension.kind = judge.kind;
      dimension.aggregation = judge.aggregation;
      dimension.threshold = judge.threshold;
      dimension.weight = judge.weight;
      dimension.judgeModel = judge.judgeModel;
      dimension.judgeFiles = judge.judgeFiles;
      dimension.judgeTrajectory = judge.judgeTrajectory;
      dimension.criteria.push(...judge.criteria);
      dimension.source = file.path;
    } else if (base.endsWith(".py")) {
      const parsed = parsePythonCriteria(file.content, file.path);
      if (parsed.criteria.length === 0 && parsed.complete) continue;
      sources.push(file.path);
      const name = dimensionNameFor(file.path, testsPrefix);
      const dimension = ensureDimension(name, file.path);
      dimension.criteria.push(...parsed.criteria);
      if (!parsed.complete) dimension.criteriaComplete = false;
      // Programmatic dimension weight = sum of its criteria weights.
      dimension.weight = dimension.criteria.reduce(
        (sum, c) => sum + c.weight,
        0
      );
    }
  }

  const aggregates: RewardDesignAggregate[] = [];
  if (rewardToml) {
    sources.unshift(rewardToml.path);
    const doc = safeParseToml(rewardToml.content);
    if (doc && Array.isArray(doc.reward)) {
      for (const entry of doc.reward) {
        if (!isRecord(entry)) continue;
        const name = nonEmptyString(entry.name);
        if (!name) continue;
        aggregates.push({
          name,
          aggregation: parseAggregation(entry.aggregation),
          threshold: finiteNumber(entry.threshold) ?? 0.5,
        });
      }
    }
  }

  const dimensions = [...byDimension.values()];
  if (dimensions.length === 0 && aggregates.length === 0) return null;
  return { dimensions, aggregates, sources };
}

/**
 * Fold observed criteria (from a trial's reward-details) into a static
 * design: python dimensions whose criteria could not be fully enumerated
 * get the exact names/weights the verifier actually scored.
 */
export function enrichDesignWithObserved(
  design: RewardDesign,
  observed: RewardBreakdown
): RewardDesign {
  const observedByName = new Map<string, RewardDimensionResult>();
  for (const dimension of observed.dimensions) {
    if (!observedByName.has(dimension.name)) {
      observedByName.set(dimension.name, dimension);
    }
  }

  const dimensions = design.dimensions.map((dimension) => {
    const seen = observedByName.get(dimension.name);
    observedByName.delete(dimension.name);
    if (!seen || seen.criteria.length === 0) return dimension;
    const known = new Set(dimension.criteria.map((c) => c.name));
    const fromObserved = seen.criteria
      .filter((c) => !known.has(c.name))
      .map<RewardDesignCriterion>((c) => ({
        name: c.name,
        description: c.description ?? null,
        type: null,
        weight: c.weight,
        optional: c.optional === true,
        source: "observed in trial results",
      }));
    if (fromObserved.length === 0 && dimension.criteriaComplete) {
      return dimension;
    }
    const criteria = [...dimension.criteria, ...fromObserved];
    return {
      ...dimension,
      criteria,
      criteriaComplete: true,
      weight:
        dimension.kind === "programmatic"
          ? criteria.reduce((sum, c) => sum + c.weight, 0)
          : dimension.weight,
    };
  });

  // Dimensions the static scan missed entirely but trials scored.
  for (const [name, seen] of observedByName) {
    dimensions.push({
      name,
      kind: seen.kind ?? "programmatic",
      aggregation: "weighted_mean",
      threshold: 0.5,
      weight: seen.criteria.reduce((sum, c) => sum + c.weight, 0) || 1,
      judgeModel: seen.judge,
      judgeFiles: seen.judgeFiles,
      judgeTrajectory: seen.judgeTrajectory,
      criteria: seen.criteria.map((c) => ({
        name: c.name,
        description: c.description ?? null,
        type: null,
        weight: c.weight,
        optional: c.optional === true,
        source: "observed in trial results",
      })),
      criteriaComplete: true,
      source: "observed in trial results",
    });
  }

  return { ...design, dimensions };
}

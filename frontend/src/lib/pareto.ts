import type { Task, Trial } from "@/lib/types";
import {
  getExperimentAgentKey,
  getModelScopedAgentsFromSummaries,
  type ExperimentAgentSummary,
} from "@/lib/experiment-agent-grouping";
import { passAtOneFraction } from "@/lib/pass-at-k";
import { trialDurationSec } from "@/lib/format";

function trialTokens(trial: Trial): number | null {
  if (trial.input_tokens == null && trial.output_tokens == null) return null;
  return (trial.input_tokens ?? 0) + (trial.output_tokens ?? 0);
}

function trialSeconds(trial: Trial): number | null {
  // Prefer the agent's own trajectory clock; wall clock (started→finished)
  // is the fallback and folds in environment setup and verification.
  return trial.trajectory_duration_seconds ?? trialDurationSec(trial);
}

// Adding a metric is one entry here plus a display entry in the graph's
// METRIC_DEFS; aggregation, availability, and the toggle row all follow.
const METRIC_EXTRACTORS = {
  cost: (trial: Trial) => trial.cost_usd ?? null,
  tokens: trialTokens,
  time: trialSeconds,
  steps: (trial: Trial) => trial.total_steps ?? null,
  tools: (trial: Trial) => trial.total_tool_calls ?? null,
} as const;

export type ParetoMetric = keyof typeof METRIC_EXTRACTORS | "costPerSuccess";

// Display order, with the derived cost-per-success slotted beside cost. The
// graph's METRIC_DEFS record is keyed on the full union, so a metric missing
// a display entry fails to compile.
export const PARETO_METRICS: readonly ParetoMetric[] = [
  "cost",
  "costPerSuccess",
  "tokens",
  "time",
  "steps",
  "tools",
];

const EXTRACTED_METRICS = Object.keys(
  METRIC_EXTRACTORS
) as (keyof typeof METRIC_EXTRACTORS)[];

type MetricAggregate = {
  /** The plotted value: mean per trial for extracted metrics, dollars per
   *  passing trial for costPerSuccess. */
  value: number;
  /** Trials the value was computed from (drives the coverage note). */
  trialCount: number;
};

export type AgentParetoPoint = {
  key: string;
  label: string;
  /**
   * Mean over tasks of the per-task pass@1 fraction — computed with the same
   * shared estimator the leaderboard ranks by, so the y-axis agrees with it.
   */
  score: number;
  /** Tasks contributing to the score mean. */
  taskCount: number;
  /** Trials behind the score (every trial of the agent across those tasks). */
  trialCount: number;
  metrics: Record<ParetoMetric, MetricAggregate | null>;
  /** Estimate provenance for the cost metric (drives the ~ / * marks). */
  costHasEstimated: boolean;
  costHasNative: boolean;
};

/**
 * One score/cost/tokens/time/steps/tools point per agent, aggregated over
 * every task the agent ran. Trials group by the same experiment agent key as
 * the pass/k graph and leaderboard, so all three cards describe the same
 * cohorts.
 */
export function buildAgentParetoPoints(
  tasks: Task[],
  agentSummaries: ExperimentAgentSummary[]
): AgentParetoPoint[] {
  const modelScopedAgents = getModelScopedAgentsFromSummaries(agentSummaries);

  // agent key → task id → that agent's trials on that task
  const trialsByAgent = new Map<string, Map<string, Trial[]>>();
  for (const task of tasks) {
    for (const trial of task.trials ?? []) {
      const key = getExperimentAgentKey(trial, modelScopedAgents);
      const perTask = trialsByAgent.get(key) ?? new Map<string, Trial[]>();
      perTask.set(task.id, [...(perTask.get(task.id) ?? []), trial]);
      trialsByAgent.set(key, perTask);
    }
  }

  const points: AgentParetoPoint[] = [];
  for (const summary of agentSummaries) {
    const perTask = trialsByAgent.get(summary.key);
    if (!perTask) continue;

    const sums = {} as Record<
      keyof typeof METRIC_EXTRACTORS,
      { sum: number; count: number }
    >;
    for (const metric of EXTRACTED_METRICS) sums[metric] = { sum: 0, count: 0 };
    let costHasEstimated = false;
    let costHasNative = false;
    let pricedPasses = 0;
    let scoreSum = 0;
    let trialCount = 0;

    for (const trials of perTask.values()) {
      // Task buckets are never empty, so the fraction is never null.
      scoreSum += passAtOneFraction(trials) ?? 0;
      trialCount += trials.length;
      for (const trial of trials) {
        for (const metric of EXTRACTED_METRICS) {
          const value = METRIC_EXTRACTORS[metric](trial);
          if (value == null) continue;
          sums[metric].sum += value;
          sums[metric].count += 1;
        }
        if (trial.cost_usd != null) {
          if (trial.reward === 1) pricedPasses += 1;
          if (trial.cost_is_estimated === true) costHasEstimated = true;
          else costHasNative = true;
        }
      }
    }

    const metrics = {} as Record<ParetoMetric, MetricAggregate | null>;
    for (const metric of EXTRACTED_METRICS) {
      const { sum, count } = sums[metric];
      metrics[metric] =
        count > 0 ? { value: sum / count, trialCount: count } : null;
    }
    // Derived: what one solved task costs — total spend over priced trials
    // divided by the passes among them. Null (toggle disabled) until the
    // agent has a priced passing trial.
    metrics.costPerSuccess =
      pricedPasses > 0
        ? { value: sums.cost.sum / pricedPasses, trialCount: sums.cost.count }
        : null;

    points.push({
      key: summary.key,
      label: summary.label,
      score: scoreSum / perTask.size,
      taskCount: perTask.size,
      trialCount,
      metrics,
      costHasEstimated,
      costHasNative,
    });
  }

  return points;
}

/**
 * The Pareto frontier for minimize-x / maximize-y: points no other point
 * weakly dominates (lower-or-equal x with higher-or-equal y, one strict).
 * Returned in ascending-x order, ready to draw as a line.
 */
export function paretoFrontier<T>(
  points: readonly T[],
  getX: (point: T) => number,
  getY: (point: T) => number
): T[] {
  const sorted = [...points].sort(
    (a, b) => getX(a) - getX(b) || getY(b) - getY(a)
  );
  const frontier: T[] = [];
  let bestY = -Infinity;
  for (const point of sorted) {
    if (getY(point) > bestY) {
      frontier.push(point);
      bestY = getY(point);
    }
  }
  return frontier;
}

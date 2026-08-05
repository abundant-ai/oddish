import type { Task, Trial } from "@/lib/types";
import {
  getExperimentAgentKey,
  type ExperimentAgentSummary,
} from "@/lib/experiment-agent-grouping";
import { trialDurationSec } from "@/lib/format";

export type ParetoMetric = "cost" | "tokens" | "time" | "steps" | "tools";

export const PARETO_METRICS: readonly ParetoMetric[] = [
  "cost",
  "tokens",
  "time",
  "steps",
  "tools",
];

type MetricAggregate = {
  /** Mean of the metric per trial, over the trials that reported it. */
  perTrial: number;
  /** How many trials reported the metric (the mean's denominator). */
  trialCount: number;
};

export type AgentParetoPoint = {
  key: string;
  /**
   * Mean over tasks of the per-task pass fraction (reward === 1 over all of
   * the agent's trials for that task, skips counted as non-passes) — the same
   * pass@1 estimator the leaderboard ranks by, so the y-axis agrees with it.
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

function trialTokens(trial: Trial): number | null {
  if (trial.input_tokens == null && trial.output_tokens == null) return null;
  return (trial.input_tokens ?? 0) + (trial.output_tokens ?? 0);
}

function trialSeconds(trial: Trial): number | null {
  // Prefer the agent's own trajectory clock; wall clock (started→finished)
  // is the fallback and folds in environment setup and verification.
  if (trial.trajectory_duration_seconds != null) {
    return trial.trajectory_duration_seconds;
  }
  return trialDurationSec(trial);
}

/**
 * One score/cost/tokens/time point per agent, aggregated over every task the
 * agent ran. Trials group by the same experiment agent key as the pass/k
 * graph and leaderboard, so all three cards describe the same cohorts.
 */
export function buildAgentParetoPoints(
  tasks: Task[],
  agentSummaries: ExperimentAgentSummary[]
): AgentParetoPoint[] {
  const modelScopedAgents = new Set(
    agentSummaries
      .filter((summary) => summary.isModelScoped)
      .map((summary) => summary.agent)
  );

  const trialsByAgent = new Map<string, { taskId: string; trial: Trial }[]>();
  for (const task of tasks) {
    for (const trial of task.trials ?? []) {
      const key = getExperimentAgentKey(trial, modelScopedAgents);
      const existing = trialsByAgent.get(key) ?? [];
      existing.push({ taskId: task.id, trial });
      trialsByAgent.set(key, existing);
    }
  }

  const points: AgentParetoPoint[] = [];
  for (const summary of agentSummaries) {
    const rows = trialsByAgent.get(summary.key) ?? [];
    if (rows.length === 0) continue;

    const perTask = new Map<string, { n: number; c: number }>();
    let costSum = 0;
    let costCount = 0;
    let costHasEstimated = false;
    let costHasNative = false;
    let tokenSum = 0;
    let tokenCount = 0;
    let secondsSum = 0;
    let secondsCount = 0;
    let stepsSum = 0;
    let stepsCount = 0;
    let toolsSum = 0;
    let toolsCount = 0;

    for (const { taskId, trial } of rows) {
      const bucket = perTask.get(taskId) ?? { n: 0, c: 0 };
      bucket.n += 1;
      if (trial.reward === 1) bucket.c += 1;
      perTask.set(taskId, bucket);

      if (trial.cost_usd != null) {
        costSum += trial.cost_usd;
        costCount += 1;
        if (trial.cost_is_estimated === true) costHasEstimated = true;
        else costHasNative = true;
      }
      const tokens = trialTokens(trial);
      if (tokens != null) {
        tokenSum += tokens;
        tokenCount += 1;
      }
      const seconds = trialSeconds(trial);
      if (seconds != null) {
        secondsSum += seconds;
        secondsCount += 1;
      }
      if (trial.total_steps != null) {
        stepsSum += trial.total_steps;
        stepsCount += 1;
      }
      if (trial.total_tool_calls != null) {
        toolsSum += trial.total_tool_calls;
        toolsCount += 1;
      }
    }

    let scoreSum = 0;
    for (const { n, c } of perTask.values()) scoreSum += c / n;

    points.push({
      key: summary.key,
      score: scoreSum / perTask.size,
      taskCount: perTask.size,
      trialCount: rows.length,
      metrics: {
        cost:
          costCount > 0
            ? { perTrial: costSum / costCount, trialCount: costCount }
            : null,
        tokens:
          tokenCount > 0
            ? { perTrial: tokenSum / tokenCount, trialCount: tokenCount }
            : null,
        time:
          secondsCount > 0
            ? { perTrial: secondsSum / secondsCount, trialCount: secondsCount }
            : null,
        steps:
          stepsCount > 0
            ? { perTrial: stepsSum / stepsCount, trialCount: stepsCount }
            : null,
        tools:
          toolsCount > 0
            ? { perTrial: toolsSum / toolsCount, trialCount: toolsCount }
            : null,
      },
      costHasEstimated,
      costHasNative,
    });
  }

  return points;
}

/**
 * The Pareto frontier for minimize-x / maximize-y: points no other point
 * weakly dominates (lower-or-equal x with higher-or-equal y, one strict).
 * Returned in ascending-x order, ready to draw as a step line.
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

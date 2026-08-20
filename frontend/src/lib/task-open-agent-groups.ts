import {
  getExperimentAgentDisplay,
  getExperimentAgentKey,
  getExperimentModelScopedAgents,
} from "@/lib/experiment-agent-grouping";
import type { TaskOpenAgentModelSummary, Trial } from "@/lib/types";

export interface TaskOpenAgentCard {
  key: string;
  label: string;
  summary: TaskOpenAgentModelSummary;
  trials: Trial[];
}

export interface TaskOpenAgentGroups {
  agentCards: TaskOpenAgentCard[];
  modelScopedAgents: Set<string>;
  realAgentCount: number;
  realTrialCount: number;
}

const SUM_FIELDS = [
  "trial_count",
  "completed_count",
  "failed_count",
  "skipped_count",
  "pending_count",
  "pass_count",
  "partial_count",
  "fail_count",
  "reward_sum",
  "reward_total",
  "cost_usd",
  "cost_trial_count",
  "billed_cost_usd",
  "billed_trial_count",
  "duration_sum_seconds",
  "duration_trial_count",
] as const satisfies ReadonlyArray<keyof TaskOpenAgentModelSummary>;

const OR_FIELDS = [
  "cost_has_estimated",
  "cost_has_native",
  "billed_has_estimated",
  "billed_has_native",
] as const satisfies ReadonlyArray<keyof TaskOpenAgentModelSummary>;

function emptySummary(
  source: TaskOpenAgentModelSummary
): TaskOpenAgentModelSummary {
  const display = getExperimentAgentDisplay(source);
  return {
    ...source,
    agent: source.is_probe ? "probe" : display.agent,
    model: source.is_probe ? null : display.model,
    providers: [],
    trial_count: 0,
    completed_count: 0,
    failed_count: 0,
    skipped_count: 0,
    pending_count: 0,
    pass_count: 0,
    partial_count: 0,
    fail_count: 0,
    reward_sum: 0,
    reward_total: 0,
    cost_usd: 0,
    cost_trial_count: 0,
    cost_has_estimated: false,
    cost_has_native: false,
    billed_cost_usd: 0,
    billed_trial_count: 0,
    billed_has_estimated: false,
    billed_has_native: false,
    last_run_at: null,
    duration_sum_seconds: 0,
    duration_trial_count: 0,
  };
}

function mergeSummary(
  target: TaskOpenAgentModelSummary,
  source: TaskOpenAgentModelSummary
): void {
  for (const provider of source.providers) {
    if (!target.providers.includes(provider)) target.providers.push(provider);
  }
  for (const field of SUM_FIELDS) {
    target[field] += source[field];
  }
  for (const field of OR_FIELDS) {
    target[field] ||= source[field];
  }
  if (
    source.last_run_at &&
    (!target.last_run_at || source.last_run_at > target.last_run_at)
  ) {
    target.last_run_at = source.last_run_at;
  }
}

export function buildTaskOpenAgentGroups(
  exactSummaries: TaskOpenAgentModelSummary[],
  previewTrials: Trial[]
): TaskOpenAgentGroups {
  const modelScopedAgents = getExperimentModelScopedAgents([
    ...exactSummaries,
    ...previewTrials,
  ]);
  const cards = new Map<string, TaskOpenAgentCard>();

  for (const source of exactSummaries) {
    const key = getExperimentAgentKey(source, modelScopedAgents);
    let card = cards.get(key);
    if (!card) {
      card = { key, label: key, summary: emptySummary(source), trials: [] };
      cards.set(key, card);
    }
    mergeSummary(card.summary, source);
  }

  for (const trial of previewTrials) {
    const key = getExperimentAgentKey(trial, modelScopedAgents);
    cards.get(key)?.trials.push(trial);
  }

  const agentCards = Array.from(cards.values());
  for (const card of agentCards) card.summary.providers.sort();
  return {
    agentCards,
    modelScopedAgents,
    realAgentCount: agentCards.filter((card) => !card.summary.is_probe).length,
    realTrialCount: agentCards.reduce(
      (count, card) =>
        count + (card.summary.is_probe ? 0 : card.summary.trial_count),
      0
    ),
  };
}

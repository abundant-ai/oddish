import type { Trial } from "@/lib/types";

export type TrialAggregate = {
  trialCount: number;
  completed: number;
  failed: number;
  passCount: number;
  partialCount: number;
  failCount: number;
  harnessErrorCount: number;
  pendingCount: number;
  rewardSum: number;
  rewardTotal: number;
  costUsd: number;
  costTrialCount: number;
  costHasEstimated: boolean;
  costHasNative: boolean;
  lastRunAt: string | null;
};

export const EMPTY_TRIAL_AGGREGATE: TrialAggregate = {
  trialCount: 0,
  completed: 0,
  failed: 0,
  passCount: 0,
  partialCount: 0,
  failCount: 0,
  harnessErrorCount: 0,
  pendingCount: 0,
  rewardSum: 0,
  rewardTotal: 0,
  costUsd: 0,
  costTrialCount: 0,
  costHasEstimated: false,
  costHasNative: false,
  lastRunAt: null,
};

export function accumulateTrial(acc: TrialAggregate, trial: Trial): void {
  acc.trialCount += 1;
  if (trial.cost_usd != null) {
    acc.costUsd += trial.cost_usd;
    acc.costTrialCount += 1;
    if (trial.cost_is_estimated === true) acc.costHasEstimated = true;
    else acc.costHasNative = true;
  }
  if (trial.status === "success") acc.completed += 1;
  else if (trial.status === "failed") acc.failed += 1;

  if (trial.status === "success" && trial.reward != null) {
    acc.rewardSum += trial.reward;
    acc.rewardTotal += 1;
    if (trial.reward === 1) acc.passCount += 1;
    else if (trial.reward === 0) acc.failCount += 1;
    else acc.partialCount += 1;
  } else if (trial.status === "failed") {
    acc.harnessErrorCount += 1;
  } else if (trial.status !== "success") {
    acc.pendingCount += 1;
  }

  const candidate = trial.finished_at || trial.started_at || trial.created_at;
  if (candidate && (acc.lastRunAt == null || candidate > acc.lastRunAt)) {
    acc.lastRunAt = candidate;
  }
}

export function summarizeTrials(trials: Trial[]): TrialAggregate {
  const acc: TrialAggregate = { ...EMPTY_TRIAL_AGGREGATE };
  for (const trial of trials) accumulateTrial(acc, trial);
  return acc;
}

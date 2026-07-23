import type { Trial } from "@/lib/types";

export type TrialAggregate = {
  trialCount: number;
  completed: number;
  failed: number;
  skipped: number;
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
  // Composite spend (additive; costUsd stays inference-only). qa = analysis
  // ledger, compute = modal ledger, total = inference + qa + compute. The
  // owned_*/billed_* variants mirror the owned/billed inference split below.
  qaCostUsd: number;
  computeCostUsd: number;
  totalCostUsd: number;
  ownedQaCostUsd: number;
  ownedComputeCostUsd: number;
  ownedTotalCostUsd: number;
  billedQaCostUsd: number;
  billedComputeCostUsd: number;
  billedTotalCostUsd: number;
  ownedCostUsd: number;
  ownedTrialCount: number;
  ownedHasEstimated: boolean;
  ownedHasNative: boolean;
  tokenCount: number;
  tokenTrialCount: number;
  ownedTokenCount: number;
  ownedTokenTrialCount: number;
  billedCostUsd: number;
  billedTrialCount: number;
  billedHasEstimated: boolean;
  billedHasNative: boolean;
  billedTokenCount: number;
  billedTokenTrialCount: number;
  lastRunAt: string | null;
};

export const EMPTY_TRIAL_AGGREGATE: TrialAggregate = {
  trialCount: 0,
  completed: 0,
  failed: 0,
  skipped: 0,
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
  qaCostUsd: 0,
  computeCostUsd: 0,
  totalCostUsd: 0,
  ownedQaCostUsd: 0,
  ownedComputeCostUsd: 0,
  ownedTotalCostUsd: 0,
  billedQaCostUsd: 0,
  billedComputeCostUsd: 0,
  billedTotalCostUsd: 0,
  ownedCostUsd: 0,
  ownedTrialCount: 0,
  ownedHasEstimated: false,
  ownedHasNative: false,
  tokenCount: 0,
  tokenTrialCount: 0,
  ownedTokenCount: 0,
  ownedTokenTrialCount: 0,
  billedCostUsd: 0,
  billedTrialCount: 0,
  billedHasEstimated: false,
  billedHasNative: false,
  billedTokenCount: 0,
  billedTokenTrialCount: 0,
  lastRunAt: null,
};

// Every trial counts toward cost/token totals — they price the work being
// displayed. owned=false (trials rendered under an experiment that doesn't
// own them: gathered/shared-task rows) keeps the trial out of the owned
// ("New spend") and billed totals, which stay additive across experiments.
export function accumulateTrial(
  acc: TrialAggregate,
  trial: Trial,
  owned = true,
): void {
  acc.trialCount += 1;
  if (trial.input_tokens != null || trial.output_tokens != null) {
    const tokenCount = (trial.input_tokens ?? 0) + (trial.output_tokens ?? 0);
    acc.tokenCount += tokenCount;
    acc.tokenTrialCount += 1;
    if (owned) {
      acc.ownedTokenCount += tokenCount;
      acc.ownedTokenTrialCount += 1;
      if (trial.is_billed) {
        acc.billedTokenCount += tokenCount;
        acc.billedTokenTrialCount += 1;
      }
    }
  }
  // QA + compute are separate ledgers, not gated on inference being priced, so
  // the total folds inference (0 when unpriced) back in per trial. Mirrors the
  // server-side task-detail composite fold. A trial counts toward costTrialCount
  // (and the owned/billed variants) when it contributes ANY composite component
  // -- priced inference OR QA OR compute -- so a $0-inference trial with real
  // QA/sandbox spend is counted and its spend folded, and the count never
  // contradicts the composite total. The owned/billed subsets follow the same
  // gating as the inference split (owned unless rendered under an experiment that
  // doesn't own it; billed within owned).
  const inference = trial.cost_usd ?? 0;
  const qaCost = trial.qa_cost_usd ?? 0;
  const computeCost = trial.compute_cost_usd ?? 0;
  const hasInference = trial.cost_usd != null;
  const contributesCost = hasInference || qaCost > 0 || computeCost > 0;
  const compositeCost = inference + qaCost + computeCost;
  if (hasInference) {
    acc.costUsd += inference;
    if (trial.cost_is_estimated === true) acc.costHasEstimated = true;
    else acc.costHasNative = true;
  }
  acc.qaCostUsd += qaCost;
  acc.computeCostUsd += computeCost;
  acc.totalCostUsd += compositeCost;
  if (contributesCost) acc.costTrialCount += 1;
  if (owned) {
    if (hasInference) {
      acc.ownedCostUsd += inference;
      if (trial.cost_is_estimated === true) acc.ownedHasEstimated = true;
      else acc.ownedHasNative = true;
    }
    acc.ownedQaCostUsd += qaCost;
    acc.ownedComputeCostUsd += computeCost;
    acc.ownedTotalCostUsd += compositeCost;
    if (contributesCost) acc.ownedTrialCount += 1;
    if (trial.is_billed) {
      if (hasInference) {
        acc.billedCostUsd += inference;
        if (trial.cost_is_estimated === true) acc.billedHasEstimated = true;
        else acc.billedHasNative = true;
      }
      acc.billedQaCostUsd += qaCost;
      acc.billedComputeCostUsd += computeCost;
      acc.billedTotalCostUsd += compositeCost;
      if (contributesCost) acc.billedTrialCount += 1;
    }
  }
  if (trial.status === "success") acc.completed += 1;
  else if (trial.status === "failed") acc.failed += 1;
  else if (trial.status === "skipped") acc.skipped += 1;

  if (trial.status === "success" && trial.reward != null) {
    acc.rewardSum += trial.reward;
    acc.rewardTotal += 1;
    if (trial.reward === 1) acc.passCount += 1;
    else if (trial.reward === 0) acc.failCount += 1;
    else acc.partialCount += 1;
  } else if (trial.status === "failed") {
    acc.harnessErrorCount += 1;
  } else if (trial.status === "skipped") {
    // Never ran -> counted only as `skipped` above; not pending, not an error.
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

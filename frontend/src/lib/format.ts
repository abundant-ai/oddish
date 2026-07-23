import type { Trial } from "@/lib/types";

export function formatCostUsd(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "$0.00";
  if (value < 0.01) return `$${value.toFixed(4)}`;
  if (value < 1) return `$${value.toFixed(3)}`;
  if (value < 100) return `$${value.toFixed(2)}`;
  return `$${value.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
}

// Full-precision variant for breakdown tooltips. Unlike formatCostUsd it never
// rounds large values to whole dollars, so a $1,234.5 component still reads its
// cents; sub-cent values keep four decimals.
export function formatCostUsdExact(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "$0.00";
  if (value < 0.01) return `$${value.toFixed(4)}`;
  return `$${value.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export function formatTokenCount(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0 tokens";
  const rounded = Math.round(value);
  if (rounded >= 1e9) return `${(rounded / 1e9).toFixed(1)}B tokens`;
  if (rounded >= 1e6) return `${(rounded / 1e6).toFixed(1)}M tokens`;
  if (rounded >= 1e3) return `${(rounded / 1e3).toFixed(1)}k tokens`;
  return `${rounded.toLocaleString()} tokens`;
}

export interface TaskTrialCost {
  // Inference (agent LLM) spend — kept for back-compat as the standalone
  // inference component. ``totalCostUsd`` is the composite headline.
  costUsd: number;
  // Composite components summed over every shown trial (not just priced ones):
  // qa = analysis-classifier spend, compute = Modal sandbox spend, total =
  // inference + qa + compute.
  qaCostUsd: number;
  computeCostUsd: number;
  totalCostUsd: number;
  // Trials with priced inference (drives the ~/* estimate marks).
  pricedCount: number;
  // Trials contributing ANY composite component (priced inference OR QA OR
  // compute) — the denominator for "N trials" alongside ``totalCostUsd``, so a
  // count never contradicts the shown composite total.
  contributingCount: number;
  hasEstimated: boolean;
  hasNative: boolean;
}

// Non-probe, non-superseded trials — same scope as the experiment header and
// /tasks rollup, so retries don't double-count. Gathered/shared-task trials count
// too: like the experiment Cost tile, this prices the work being displayed,
// wherever it ran. QA + compute are summed over EVERY such trial (not just the
// priced ones), matching the server-side task-detail composite fold, so
// ``totalCostUsd`` is the full inference + QA + sandbox spend and a trial whose
// inference was never priced still contributes its real QA/sandbox spend.
export function sumTaskTrialCost(
  trials: Trial[] | null | undefined,
): TaskTrialCost {
  let costUsd = 0;
  let qaCostUsd = 0;
  let computeCostUsd = 0;
  let pricedCount = 0;
  let contributingCount = 0;
  let hasEstimated = false;
  let hasNative = false;
  for (const trial of trials ?? []) {
    if (trial.is_probe) continue;
    if (trial.superseded_by_trial_id) continue;
    const qaCost = trial.qa_cost_usd ?? 0;
    const computeCost = trial.compute_cost_usd ?? 0;
    const hasInference = trial.cost_usd != null;
    if (hasInference) {
      costUsd += trial.cost_usd ?? 0;
      pricedCount += 1;
      if (trial.cost_is_estimated) hasEstimated = true;
      else hasNative = true;
    }
    qaCostUsd += qaCost;
    computeCostUsd += computeCost;
    if (hasInference || qaCost > 0 || computeCost > 0) contributingCount += 1;
  }
  return {
    costUsd,
    qaCostUsd,
    computeCostUsd,
    totalCostUsd: costUsd + qaCostUsd + computeCostUsd,
    pricedCount,
    contributingCount,
    hasEstimated,
    hasNative,
  };
}

// Estimate markers matching the experiment header (#599): "~" prefix when every
// priced trial was token-estimated, "*" suffix when native and estimated are
// mixed. Both empty when all native.
export function costEstimateMarks(
  hasEstimated: boolean,
  hasNative: boolean,
): { prefix: string; suffix: string } {
  return {
    prefix: hasEstimated && !hasNative ? "~" : "",
    suffix: hasEstimated && hasNative ? "*" : "",
  };
}

export function formatDurationSec(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "—";
  if (seconds < 1) return `${(seconds * 1000).toFixed(0)}ms`;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds - m * 60);
  if (m < 60) return s ? `${m}m ${s}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const rem = m - h * 60;
  return rem ? `${h}h ${rem}m` : `${h}h`;
}

export function trialDurationSec(trial: Trial): number | null {
  if (!trial.started_at || !trial.finished_at) return null;
  const start = new Date(trial.started_at).getTime();
  const end = new Date(trial.finished_at).getTime();
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start)
    return null;
  return (end - start) / 1000;
}

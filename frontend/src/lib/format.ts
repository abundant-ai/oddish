import type { Trial } from "@/lib/types";

export function formatCostUsd(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "$0.00";
  if (value < 0.01) return `$${value.toFixed(4)}`;
  if (value < 1) return `$${value.toFixed(3)}`;
  if (value < 100) return `$${value.toFixed(2)}`;
  return `$${value.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
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
  costUsd: number;
  pricedCount: number;
  hasEstimated: boolean;
  hasNative: boolean;
}

// Priced, non-probe, non-superseded trials only — same scope as the experiment
// header and /tasks rollup, so retries don't double-count.
export function sumTaskTrialCost(
  trials: Trial[] | null | undefined,
): TaskTrialCost {
  let costUsd = 0;
  let pricedCount = 0;
  let hasEstimated = false;
  let hasNative = false;
  for (const trial of trials ?? []) {
    if (trial.is_probe) continue;
    if (trial.superseded_by_trial_id) continue;
    if (trial.cost_usd == null) continue;
    costUsd += trial.cost_usd;
    pricedCount += 1;
    if (trial.cost_is_estimated) hasEstimated = true;
    else hasNative = true;
  }
  return { costUsd, pricedCount, hasEstimated, hasNative };
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

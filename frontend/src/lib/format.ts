import type { Trial } from "@/lib/types";

export function formatCostUsd(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "$0.00";
  if (value < 0.01) return `$${value.toFixed(4)}`;
  if (value < 1) return `$${value.toFixed(3)}`;
  if (value < 100) return `$${value.toFixed(2)}`;
  return `$${value.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
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
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return null;
  return (end - start) / 1000;
}

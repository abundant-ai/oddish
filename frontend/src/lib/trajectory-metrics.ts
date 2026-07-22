import type { TrajectoryStep } from "@/lib/types";

/**
 * Per-step duration in ms, derived from timestamps (time since the previous
 * step), index-aligned with `steps`. Mirrors TrajectoryViewer's StepDurationBar
 * so the timeline agrees with the existing duration bar. Steps with no usable
 * timestamps yield 0; a caller seeing an all-zero total should fall back to
 * equal widths.
 */
export function stepDurationsMs(steps: TrajectoryStep[]): number[] {
  return steps.map((step, idx) => {
    const t = step.timestamp ? new Date(step.timestamp).getTime() : 0;
    const prev = idx > 0 ? steps[idx - 1] : null;
    const pt = prev?.timestamp ? new Date(prev.timestamp).getTime() : t;
    return Math.max(0, t - pt);
  });
}

/** Human duration for component/run spans: "850ms", "48s", "5m 40s", "1h 12m". */
export function fmtDurationMs(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return s % 60 ? `${m}m ${s % 60}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return m % 60 ? `${h}h ${m % 60}m` : `${h}h`;
}

/** Token volume for a step (prompt + completion). Null when no token data. */
export function stepTokens(step: TrajectoryStep): number | null {
  const m = step.metrics;
  if (!m) return null;
  if (m.prompt_tokens == null && m.completion_tokens == null) return null;
  return (m.prompt_tokens ?? 0) + (m.completion_tokens ?? 0);
}

const PHASE_SLOTS = 8;

/**
 * Map each distinct phase label (first-appearance order) to a categorical color
 * CSS variable. Slots 1–8 come from the validated palette; the 9th+ distinct
 * label folds into the neutral `--phase-other` (never a cycled/generated hue).
 */
export function phaseColorVars(labels: string[]): Map<string, string> {
  const seen: string[] = [];
  for (const l of labels) if (!seen.includes(l)) seen.push(l);
  const map = new Map<string, string>();
  seen.forEach((l, i) => {
    map.set(
      l,
      i < PHASE_SLOTS ? `var(--phase-${i + 1})` : "var(--phase-other)"
    );
  });
  return map;
}

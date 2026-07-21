import type { TrajectoryComponentKind, TrajectoryStep } from "@/lib/types";

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

/** Token volume for a step (prompt + completion). Null when no token data. */
export function stepTokens(step: TrajectoryStep): number | null {
  const m = step.metrics;
  if (!m) return null;
  if (m.prompt_tokens == null && m.completion_tokens == null) return null;
  return (m.prompt_tokens ?? 0) + (m.completion_tokens ?? 0);
}

const COMPONENT_LABELS: Record<TrajectoryComponentKind, string> = {
  reading_files: "Reading files",
  thinking_recall: "Recalling",
  thinking_understand: "Understanding",
  thinking_hypothesize: "Hypothesizing",
  thinking_diagnose: "Diagnosing",
  implementing: "Implementing",
  writing_tests: "Writing tests",
  testing_public: "Running public tests",
  testing_custom: "Running custom tests",
  testing_custom_edge_cases: "Testing edge cases",
  debugging: "Debugging",
};

/** Display label for a taxonomy value; unknown values degrade to de-snaked text. */
export function componentLabel(kind: string): string {
  return (
    COMPONENT_LABELS[kind as TrajectoryComponentKind] ?? kind.replace(/_/g, " ")
  );
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
    map.set(l, i < PHASE_SLOTS ? `var(--phase-${i + 1})` : "var(--phase-other)");
  });
  return map;
}

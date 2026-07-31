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
 * Fixed semantic colors for the component taxonomy: thinking/analysis kinds
 * wear the cool family, building/testing kinds the warm family, so cool-vs-warm
 * alone separates "thinking" from "doing". Stable across trials — a kind keeps
 * its color regardless of appearance order. Hexes live in globals.css,
 * validated per mode against the card surfaces.
 */
const COMPONENT_COLOR_VARS: Record<string, string> = {
  reading_files: "var(--tc-reading-files)",
  thinking_recall: "var(--tc-thinking-recall)",
  thinking_understand: "var(--tc-thinking-understand)",
  thinking_hypothesize: "var(--tc-thinking-hypothesize)",
  thinking_diagnose: "var(--tc-thinking-diagnose)",
  implementing: "var(--tc-implementing)",
  writing_tests: "var(--tc-writing-tests)",
  testing_public: "var(--tc-testing-public)",
  testing_custom: "var(--tc-testing-custom)",
  testing_custom_edge_cases: "var(--tc-testing-edge)",
  debugging: "var(--tc-debugging)",
  // Synthetic bucket for steps no component claims — always neutral gray.
  other: "var(--phase-other)",
};

/**
 * Color for each distinct label: taxonomy kinds get their fixed semantic color;
 * anything else (legacy free-text phases) gets ordered slots 1–8 from the
 * validated palette, with the 9th+ folding into `--phase-other` (never a
 * cycled/generated hue).
 */
export function phaseColorVars(labels: string[]): Map<string, string> {
  const map = new Map<string, string>();
  let slot = 0;
  for (const l of labels) {
    if (map.has(l)) continue;
    const fixed = COMPONENT_COLOR_VARS[l];
    if (fixed) {
      map.set(l, fixed);
    } else {
      map.set(
        l,
        slot < PHASE_SLOTS ? `var(--phase-${slot + 1})` : "var(--phase-other)"
      );
      slot += 1;
    }
  }
  return map;
}

import type {
  ContentPart,
  MessageContent,
  ObservationContent,
  TrajectoryStep,
} from "@/lib/types";

function hasContent(content: MessageContent | ObservationContent): boolean {
  if (content == null) return false;
  if (typeof content === "string") return content.trim().length > 0;
  // An image part carries content even though it holds no text.
  return content.some((part: ContentPart) =>
    part.type === "text" ? (part.text ?? "").trim().length > 0 : true
  );
}

/**
 * A step the producer emitted but never filled in: no message, no reasoning, no
 * tool call, no observation. Some ATIF writers pad a trajectory with long runs
 * of these — one gemini-cli export was 87% empty, alternating agent/user at the
 * same millisecond — and they represent no work at all. Anything that counts,
 * measures or colors steps must drop them first, or empty padding shows up as
 * activity.
 */
export function isEmptyStep(step: TrajectoryStep): boolean {
  if (step.tool_calls?.length) return false;
  if (step.observation?.results?.some((r) => hasContent(r.content)))
    return false;
  if ((step.reasoning_content ?? "").trim().length > 0) return false;
  return !hasContent(step.message);
}

function timestampMs(ts: string | null | undefined): number | null {
  if (!ts) return null;
  const ms = new Date(ts).getTime();
  return Number.isFinite(ms) ? ms : null;
}

/**
 * Per-step duration in ms, derived from timestamps (time since the previous
 * step), index-aligned with `steps`. The single measure of step time: the
 * duration bar, the accordion's group headers and per-step badges, and the
 * Activity card all read it, so they cannot disagree. Steps with no usable
 * timestamps yield 0; a caller seeing an all-zero total should fall back to
 * equal widths.
 *
 * Empty padding is worth 0 and "previous" skips it, so the span it covers is
 * charged to the next step that did work — including a padding run at the head,
 * which is measured from the trajectory's first stamp rather than dropped.
 *
 * That makes the total honest only if callers measure the FULL trajectory and
 * narrow afterwards by original index. Filtering padding out first and measuring
 * the remainder is not equivalent: the head run has no successor left to charge,
 * so its span is silently dropped and the caller under-reports. Every caller
 * measures full and narrows, which is what keeps the duration bar, the group
 * headers and the Activity card reporting one number per component.
 */
export function stepDurationsMs(steps: TrajectoryStep[]): number[] {
  let prevMs = timestampMs(steps.find((s) => s.timestamp)?.timestamp);
  return steps.map((step) => {
    if (isEmptyStep(step)) return 0;
    const t = timestampMs(step.timestamp);
    if (t == null) return 0;
    const ms = prevMs == null ? 0 : Math.max(0, t - prevMs);
    prevMs = t;
    return ms;
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
  thinking_correction: "var(--tc-thinking-correction)",
  writing_plan: "var(--tc-writing-plan)",
  implementing: "var(--tc-implementing)",
  implementing_correction: "var(--tc-implementing-correction)",
  writing_tests: "var(--tc-writing-tests)",
  testing_public: "var(--tc-testing-public)",
  testing_custom: "var(--tc-testing-custom)",
  testing_edge_cases: "var(--tc-testing-edge)",
  debugging: "var(--tc-debugging)",
  // Retired kinds, kept so stored summaries keep their fixed color rather than
  // falling through to appearance-order phase slots. Each shares the slot its
  // replacement took over; the two vocabularies never co-occur in one summary.
  thinking_diagnose: "var(--tc-thinking-correction)",
  testing_custom_edge_cases: "var(--tc-testing-edge)",
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

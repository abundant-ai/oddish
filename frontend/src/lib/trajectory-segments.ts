import { fmtDurationMs, isEmptyStep } from "@/lib/trajectory-metrics";
import type {
  TrajectoryComponentKind,
  TrajectoryStep,
  TrajectorySummary,
} from "@/lib/types";

const COMPONENT_LABELS: Record<TrajectoryComponentKind, string> = {
  reading_files: "Reading files",
  thinking_recall: "Recalling",
  thinking_understand: "Understanding",
  thinking_hypothesize: "Hypothesizing",
  thinking_correction: "Correcting course",
  implementing: "Implementing",
  implementing_correction: "Correcting implementation",
  writing_tests: "Writing tests",
  testing_public: "Running public tests",
  testing_custom: "Running custom tests",
  testing_edge_cases: "Testing edge cases",
  debugging: "Debugging",
  thinking_diagnose: "Diagnosing",
  testing_custom_edge_cases: "Testing edge cases",
};

/** Display label for a taxonomy value; unknown values degrade to de-snaked text. */
export function componentLabel(kind: string): string {
  return (
    COMPONENT_LABELS[kind as TrajectoryComponentKind] ?? kind.replace(/_/g, " ")
  );
}

/**
 * Normalized segment of the run, from the summary's `components` (taxonomy-valued,
 * schema v4+) or its legacy free-text `phases`. `key` is what colors are assigned
 * by, so repeats of the same taxonomy value share a color.
 */
export interface Segment {
  key: string;
  label: string;
  gist: string;
  stepIds: number[];
}

/** A step paired with its index in the *full* trajectory, which search must not disturb. */
export interface IndexedStep {
  step: TrajectoryStep;
  idx: number;
}

/** A contiguous run of steps under one segment. `key: null` = claimed by none. */
export interface StepGroup {
  key: string | null;
  label: string | null;
  gist: string;
  steps: IndexedStep[];
}

export function toSegments(
  summary: TrajectorySummary | null | undefined
): Segment[] {
  if (!summary) return [];
  const sorted = (ids: number[]) => [...ids].sort((a, b) => a - b);
  if (summary.components?.length) {
    return summary.components
      .filter((c) => c.step_ids.length > 0)
      .map((c) => ({
        key: c.trajectory_component,
        label: componentLabel(c.trajectory_component),
        gist: c.summary ?? "",
        stepIds: sorted(c.step_ids),
      }));
  }
  return (summary.phases ?? [])
    .filter((p) => p.step_ids.length > 0)
    .map((p) => ({
      key: p.label,
      label: p.label,
      gist: p.gist,
      stepIds: sorted(p.step_ids),
    }));
}

/**
 * step_id -> owning segment, the one source of truth for step attribution.
 *
 * Segments are not a partition: the model can leave steps unclaimed, interleave
 * two components, or put one step_id in two components (the backend's
 * `filter_output` drops invalid ids but never dedupes). First claim wins, ordered
 * by lowest step_id, so every step is attributed exactly once — and the grouped
 * list and the Activity timeline agree on which component owns it.
 *
 * On long runs the model's claims are sparse (e.g. every other id), so a step
 * that sits between two claims of the SAME component is attributed to the
 * earlier one; a gap between differing components stays unclaimed.
 *
 * That bridge is a guess, so it is capped at MAX_GAP_FILL_STEPS. Bridging one
 * skipped step is almost certainly right; bridging two hundred is inventing a
 * component the model never claimed. Real summaries bear the cutoff out —
 * across the sampled corpus every bridged gap held either <= 2 real steps or
 * >= 14, nothing between, and the long ones were pure invention (they inflated
 * `debugging` by 147%).
 *
 * `renderableIds` is the id set the UI actually draws (i.e. minus empty padding
 * steps). It is used to MEASURE a gap, not to limit what gets filled: a bridge
 * still claims every id in the span, so a run of padding cannot shatter the
 * grouped step list into fragments. Omit it and the gap is measured in raw ids,
 * which is only right when no padding exists.
 */
export const MAX_GAP_FILL_STEPS = 5;

export function segmentOwners(
  segments: Segment[],
  renderableIds?: ReadonlySet<number>
): Map<number, Segment> {
  const ordered = segments
    .filter((s) => s.stepIds.length > 0)
    .sort((a, b) => Math.min(...a.stepIds) - Math.min(...b.stepIds));

  const owner = new Map<number, Segment>();
  for (const segment of ordered) {
    // step_id is typed number but arrives as a string from some producers.
    for (const id of segment.stepIds.map(Number)) {
      if (!owner.has(id)) owner.set(id, segment);
    }
  }

  const claimed = [...owner.keys()].sort((a, b) => a - b);
  for (let i = 1; i < claimed.length; i++) {
    const prev = claimed[i - 1];
    const next = claimed[i];
    const prevSegment = owner.get(prev)!;
    if (next - prev <= 1 || owner.get(next)!.key !== prevSegment.key) continue;

    let span = 0;
    for (let id = prev + 1; id < next; id++) {
      if (!renderableIds || renderableIds.has(id)) span += 1;
      if (span > MAX_GAP_FILL_STEPS) break;
    }
    if (span > MAX_GAP_FILL_STEPS) continue;

    for (let id = prev + 1; id < next; id++) owner.set(id, prevSegment);
  }
  return owner;
}

/** The ids the UI draws: every step that carries content. */
export function renderableStepIds(steps: TrajectoryStep[]): Set<number> {
  return new Set(
    steps.filter((s) => !isEmptyStep(s)).map((s) => Number(s.step_id))
  );
}

/** Key of the synthetic segment holding steps no component claims. */
export const OTHER_SEGMENT_KEY = "other";

/**
 * Append a synthetic gray "Other" segment covering every loaded step that no
 * component claims even after gap-fill, so coverage is complete by
 * construction for every stored summary (no regeneration). Returns the input
 * array untouched when coverage is already complete.
 */
export function withOtherSegment(
  segments: Segment[],
  steps: TrajectoryStep[],
  renderableIds?: ReadonlySet<number>
): Segment[] {
  // No segments means there is no usable summary yet (still loading, absent,
  // or failed). Preserve that state instead of presenting every step as Other.
  if (segments.length === 0) return segments;

  const ids = renderableIds ?? renderableStepIds(steps);
  const owner = segmentOwners(segments, ids);
  const unclaimed = [...ids]
    .filter((id) => !owner.has(id))
    .sort((a, b) => a - b);
  if (unclaimed.length === 0) return segments;
  return [
    ...segments,
    {
      key: OTHER_SEGMENT_KEY,
      label: "Other",
      gist: "Steps not attributed to any component",
      stepIds: unclaimed,
    },
  ];
}

/**
 * Cut `steps` into contiguous runs by owning segment, preserving step order.
 * Unclaimed runs come back with `key: null`, and an interleaved segment yields
 * one run per stretch rather than being hoisted out of order.
 */
export function groupStepsBySegment(
  steps: IndexedStep[],
  segments: Segment[],
  renderableIds?: ReadonlySet<number>
): StepGroup[] {
  // Derived from the caller's full trajectory, never from `steps` -- that list
  // is search-filtered, and attribution must not shift as the user types.
  const owner = segmentOwners(segments, renderableIds);

  const groups: StepGroup[] = [];
  let current: StepGroup | null = null;
  let currentSegment: Segment | null = null;
  for (const entry of steps) {
    // step_id is typed number but arrives as a string from some producers.
    const segment = owner.get(Number(entry.step.step_id)) ?? null;
    if (!current || segment !== currentSegment) {
      current = {
        key: segment?.key ?? null,
        label: segment?.label ?? null,
        gist: segment?.gist ?? "",
        steps: [],
      };
      groups.push(current);
      currentSegment = segment;
    }
    current.steps.push(entry);
  }
  return groups;
}

/**
 * Label a set of step IDs without implying that sparse IDs are contiguous.
 * Consecutive runs are compacted, so [1, 2, 4, 7, 8] becomes
 * "steps 1–2, 4, 7–8".
 */
export function stepIdsLabel(stepIds: number[]): string {
  const ids = [...new Set(stepIds.map(Number))].sort((a, b) => a - b);
  if (ids.length === 1) return `step ${ids[0]}`;

  const runs: string[] = [];
  let start = ids[0];
  let end = start;
  for (const id of ids.slice(1)) {
    if (id === end + 1) {
      end = id;
      continue;
    }
    runs.push(start === end ? `${start}` : `${start}–${end}`);
    start = id;
    end = id;
  }
  runs.push(start === end ? `${start}` : `${start}–${end}`);
  return `steps ${runs.join(", ")}`;
}

/** Step-ID label for a group, computed from the steps actually shown. */
export function stepRangeLabel(group: StepGroup): string {
  return stepIdsLabel(group.steps.map((s) => Number(s.step.step_id)));
}

/**
 * Header stats for a group: "steps 12–24 · 9 tools · 1m 12s". Like
 * `stepRangeLabel`, computed from the steps actually shown; `durations` is
 * index-aligned with the *full* trajectory (see `stepDurationsMs`), which the
 * group's `idx` values index into even when the list is filtered.
 */
export function groupStatsLabel(group: StepGroup, durations: number[]): string {
  const tools = group.steps.reduce(
    (sum, { step }) => sum + (step.tool_calls?.length ?? 0),
    0
  );
  const ms = group.steps.reduce(
    (sum, { idx }) => sum + (durations[idx] ?? 0),
    0
  );
  const parts = [
    stepRangeLabel(group),
    `${tools} ${tools === 1 ? "tool" : "tools"}`,
  ];
  if (ms > 0) parts.push(fmtDurationMs(ms));
  return parts.join(" · ");
}

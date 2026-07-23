import { fmtDurationMs } from "@/lib/trajectory-metrics";
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
  toolCount?: number;
  durationMs?: number;
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
        toolCount: c.tool_count,
        durationMs: c.duration_ms,
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
 */
export function segmentOwners(segments: Segment[]): Map<number, Segment> {
  const ordered = segments
    .filter((s) => s.stepIds.length > 0)
    .sort((a, b) => Math.min(...a.stepIds) - Math.min(...b.stepIds));

  const owner = new Map<number, Segment>();
  for (const segment of ordered) {
    for (const id of segment.stepIds) {
      if (!owner.has(id)) owner.set(id, segment);
    }
  }
  return owner;
}

/**
 * Cut `steps` into contiguous runs by owning segment, preserving step order.
 * Unclaimed runs come back with `key: null`, and an interleaved segment yields
 * one run per stretch rather than being hoisted out of order.
 */
export function groupStepsBySegment(
  steps: IndexedStep[],
  segments: Segment[]
): StepGroup[] {
  const owner = segmentOwners(segments);

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

import {
  fmtDurationMs,
  isEmptyStep,
  phaseColorVars,
  stepDurationsMs,
  stepTokens,
} from "@/lib/trajectory-metrics";
import {
  type Segment,
  segmentOwners,
  stepIdsLabel,
  toSegments,
  withOtherSegment,
} from "@/lib/trajectory-segments";
import type { TrajectoryStep, TrajectorySummary } from "@/lib/types";

export const STEP_BAND_TITLE = "Timeline · by steps";
export const TOKEN_BAND_TITLE = "Timeline · by tokens";
export const TIMELINE_CAPTION =
  "bars share components and order · thin band = tokens per step, darker is more · lower band = width by token volume";

/** Compact token counts so a 1.2M cell still fits the fixed-width column. */
const fmtTokens = (n: number) =>
  n >= 1_000_000
    ? `${(n / 1_000_000).toFixed(1)}M`
    : n >= 1000
      ? `${Math.round(n / 1000)}k`
      : n.toLocaleString();

const fmtMs = (ms: number) =>
  ms >= 1000
    ? `${(ms / 1000).toFixed(ms >= 10000 ? 0 : 1)}s`
    : `${Math.round(ms)}ms`;

interface InstanceStat {
  key: string;
  label: string;
  firstStepId: number;
  rangeLabel: string;
  stepCount: number;
  toolCount: number;
  tokenCount: number;
  durationMs: number;
}

interface KindStat {
  key: string;
  label: string;
  instances: InstanceStat[];
  stepCount: number;
  toolCount: number;
  tokenCount: number;
  durationMs: number;
}

interface MetricValues {
  stepCount: number;
  toolCount: number;
  tokenCount: number;
  durationMs: number;
}

interface ActivityBar {
  firstStepId: number;
  /** Share of the section's longest bar, 0–100. */
  pct: number;
  title: string;
}

interface ActivityRow {
  key: string;
  label: string;
  /** CSS color expression (a `var(--…)` token), resolved by the consumer. */
  color: string;
  valueLabel: string;
  bars: ActivityBar[];
}

interface ActivitySection {
  name: string;
  totalLabel: string;
  rows: ActivityRow[];
}

interface ActivityStep {
  stepId: number;
  title: string;
  highlighted: boolean;
  /** Flex grow factor of this step inside its run in the token band. */
  tokenFlex: number;
  heatColor: string;
  heatTitle: string;
  tokenTitle: string;
}

/** A contiguous run of steps under one component, the unit both bands size. */
interface ActivityRun {
  color: string;
  stepWeight: number;
  tokenWeight: number;
  steps: ActivityStep[];
}

export interface ActivityViewModel {
  sections: ActivitySection[];
  runs: ActivityRun[];
  hasTokens: boolean;
  /** Beside the step band's heading. */
  stepTotalLabel: string;
  /** Beside the token band's heading. */
  tokenTotalLabel: string;
  /** Set when empty padding steps were dropped from every count above. */
  emptyNote: string | null;
}

const instanceTitle = (instance: InstanceStat) =>
  [
    instance.label,
    instance.rangeLabel,
    `${instance.toolCount} ${instance.toolCount === 1 ? "tool" : "tools"}`,
    instance.durationMs > 0 ? fmtDurationMs(instance.durationMs) : "—",
  ].join(" · ");

/**
 * Everything the Activity card draws, as data. The card renders it as DOM and
 * the image exporter draws the same values onto a canvas, so the two can never
 * disagree about segment math, ordering or labels.
 *
 * Returns null when there is nothing to show (no steps, or no usable summary).
 */
export function buildActivityViewModel(
  allSteps: TrajectoryStep[],
  summary: TrajectorySummary | null | undefined
): ActivityViewModel | null {
  // Empty padding steps are dropped up front, so they cannot be counted in a
  // bar, drawn as a cell, or measured as gap-fill distance further down. Their
  // original indexes are kept: time has to be measured against the whole
  // trajectory (see `stepDurationsMs`), and only then narrowed to what is drawn.
  const kept = allSteps
    .map((step, index) => ({ step, index }))
    .filter(({ step }) => !isEmptyStep(step));
  const steps = kept.map((k) => k.step);
  const emptyCount = allSteps.length - steps.length;
  const renderableIds = new Set(steps.map((s) => Number(s.step_id)));

  const segments = withOtherSegment(toSegments(summary), steps, renderableIds);
  if (!steps.length || segments.length === 0) return null;

  const colorFor = phaseColorVars(segments.map((s) => s.key));
  const labelFor = new Map(segments.map((s) => [s.key, s.label]));
  const owner = segmentOwners(segments, renderableIds);
  // step_id is typed number but arrives as a string from some producers.
  const keyByStep = (stepId: number) => owner.get(Number(stepId))?.key;
  const highlightIds = new Set(
    (summary?.highlights ?? []).map((h) => h.step_id)
  );
  const colorOf = (key: string | undefined) =>
    colorFor.get(key ?? "") ?? "var(--phase-other)";

  const allDurations = stepDurationsMs(allSteps);
  const durations = kept.map(({ index }) => allDurations[index]);
  const totalMs = durations.reduce((a, b) => a + b, 0);
  const tokens = steps.map(stepTokens);
  const maxTok = Math.max(0, ...tokens.map((t) => t ?? 0));
  const hasTokens = maxTok > 0;
  const totalTools = steps.reduce(
    (sum, step) => sum + (step.tool_calls?.length ?? 0),
    0
  );
  const totalTokens = tokens.reduce<number>((sum, t) => sum + (t ?? 0), 0);
  // Every metric is derived from `owner` — the same attribution the timeline
  // paints with — so the bars and the strip are one partition by construction
  // and the per-kind step counts sum to steps.length. Raw stepIds cannot be
  // used: the model routinely leaves steps unclaimed despite being told to
  // cover them all, so gap-fill grants a component steps it never claimed
  // (seen inflating a single kind by >2x on sparse summaries), and the backend
  // permits one step in two components. The summary's persisted
  // tool_count/duration_ms are computed off the same raw claims, so they go too.
  const ownedIndexes = new Map<Segment, number[]>();
  steps.forEach((_, index) => {
    const segment = owner.get(Number(steps[index].step_id));
    if (!segment) return;
    const owned = ownedIndexes.get(segment);
    if (owned) owned.push(index);
    else ownedIndexes.set(segment, [index]);
  });

  const instances: InstanceStat[] = segments
    .map((segment) => {
      // Ascending, since steps are walked in trajectory order.
      const indexes = ownedIndexes.get(segment) ?? [];
      const ids = indexes.map((index) => Number(steps[index].step_id));
      return {
        key: segment.key,
        label: segment.label,
        firstStepId: ids[0],
        rangeLabel: stepIdsLabel(ids),
        stepCount: ids.length,
        toolCount: indexes.reduce(
          (sum, index) => sum + (steps[index].tool_calls?.length ?? 0),
          0
        ),
        tokenCount: indexes.reduce(
          (sum, index) => sum + (tokens[index] ?? 0),
          0
        ),
        durationMs: indexes.reduce((sum, index) => sum + durations[index], 0),
      };
    })
    // A component whose every claim was won by an earlier one owns no step and
    // has nothing to show in the legend.
    .filter((instance) => instance.stepCount > 0);

  // Instances rolled up by taxonomy kind, in first-appearance order (matching
  // color assignment); a kind's instances stay chronological within its bar.
  const kinds: KindStat[] = [];
  const kindByKey = new Map<string, KindStat>();
  for (const instance of instances) {
    let kind = kindByKey.get(instance.key);
    if (!kind) {
      kind = {
        key: instance.key,
        label: instance.label,
        instances: [],
        stepCount: 0,
        toolCount: 0,
        tokenCount: 0,
        durationMs: 0,
      };
      kindByKey.set(instance.key, kind);
      kinds.push(kind);
    }
    kind.instances.push(instance);
    kind.stepCount += instance.stepCount;
    kind.toolCount += instance.toolCount;
    kind.tokenCount += instance.tokenCount;
    kind.durationMs += instance.durationMs;
  }
  for (const kind of kinds) {
    kind.instances.sort((a, b) => a.firstStepId - b.firstStepId);
  }

  const specs: {
    name: string;
    totalLabel: string;
    value: (m: MetricValues) => number;
    fmt: (n: number) => string;
    hideWhenEmpty?: boolean;
  }[] = [
    {
      name: "Steps",
      totalLabel: steps.length.toLocaleString(),
      value: (m) => m.stepCount,
      fmt: (n) => n.toLocaleString(),
    },
    {
      // Sits beside Steps on purpose: a component with few steps but heavy
      // tokens (a big file read) reads very differently from a long cheap one.
      name: "Tokens",
      totalLabel: fmtTokens(totalTokens),
      value: (m) => m.tokenCount,
      fmt: fmtTokens,
      hideWhenEmpty: true,
    },
    {
      name: "Tool calls",
      totalLabel: totalTools.toLocaleString(),
      value: (m) => m.toolCount,
      fmt: (n) => n.toLocaleString(),
    },
    {
      name: "Time",
      totalLabel: totalMs > 0 ? fmtDurationMs(totalMs) : "—",
      value: (m) => m.durationMs,
      fmt: (n) => (n > 0 ? fmtDurationMs(n) : "—"),
      hideWhenEmpty: true,
    },
  ];

  const sections: ActivitySection[] = [];
  for (const spec of specs) {
    // Longest kind fills the track; others are proportional to it.
    const denom = Math.max(0, ...kinds.map((kind) => spec.value(kind)));
    // Nothing to plot: codex trajectories carry no timestamps, and some
    // producers report no per-step tokens.
    if (spec.hideWhenEmpty && denom <= 0) continue;
    // Largest bar first; stable sort keeps first-appearance order on ties.
    const ranked = [...kinds].sort((a, b) => spec.value(b) - spec.value(a));
    sections.push({
      name: spec.name,
      totalLabel: spec.totalLabel,
      rows: ranked.map((kind) => ({
        key: kind.key,
        label: kind.label,
        color: colorOf(kind.key),
        valueLabel: spec.fmt(spec.value(kind)),
        bars:
          denom > 0
            ? kind.instances
                .map((instance) => ({
                  firstStepId: instance.firstStepId,
                  pct: Math.min(100, (spec.value(instance) / denom) * 100),
                  title: instanceTitle(instance),
                }))
                .filter((bar) => bar.pct > 0)
            : [],
      })),
    });
  }

  // A per-cell floor plus a gap costs ~8px a step. Past ~100 steps that exceeds
  // the card, and the strip does two silent wrong things: it scrolls the tail
  // out of view (a 187-step run hid its last 20%, which is where the debugging
  // was), and every cell pins to the floor, so width stops meaning duration.
  // Dense runs therefore give up the gap and the floor to stay proportional.
  // Contiguous same-component runs. Rounding and the gap live on the RUN, not
  // the step: a per-step radius either scallops the band into a comb once cells
  // go flush, or (with a per-step gap and floor) overflows the card and scrolls
  // the tail of the run out of sight. Per-step cells still sit inside each run,
  // so hover, click and the highlight star keep step granularity.
  const grouped: { key: string | undefined; indexes: number[] }[] = [];
  steps.forEach((step, index) => {
    const key = keyByStep(step.step_id);
    const last = grouped[grouped.length - 1];
    if (last && last.key === key) last.indexes.push(index);
    else grouped.push({ key, indexes: [index] });
  });

  // The token band re-lays the same runs against token share instead of time,
  // so a component that is narrow above and wide here spent few steps and many
  // tokens. Equal widths when no step reports tokens, matching widthPct.
  const tokenPct = (i: number) =>
    totalTokens > 0
      ? ((tokens[i] ?? 0) / totalTokens) * 100
      : 100 / steps.length;

  const runs: ActivityRun[] = grouped.map((run) => ({
    color: colorOf(run.key),
    stepWeight: run.indexes.length,
    tokenWeight: run.indexes.reduce((sum, i) => sum + tokenPct(i), 0),
    steps: run.indexes.map((i) => {
      const step = steps[i];
      const tok = tokens[i] ?? 0;
      const label = labelFor.get(run.key ?? "") ?? "";
      return {
        stepId: step.step_id,
        title: `Step ${step.step_id} · ${label} · ${fmtMs(durations[i])}`,
        highlighted: highlightIds.has(step.step_id),
        tokenFlex: tokenPct(i),
        // Read only when `hasTokens`, i.e. only when maxTok > 0.
        heatColor: hasTokens
          ? `color-mix(in srgb, var(--phase-1) ${12 + Math.round((tok / maxTok) * 88)}%, transparent)`
          : "transparent",
        heatTitle: `Step ${step.step_id} · ${tok.toLocaleString()} tokens`,
        tokenTitle: `Step ${step.step_id} · ${label} · ${tok.toLocaleString()} tokens`,
      };
    }),
  }));

  return {
    sections,
    runs,
    hasTokens,
    stepTotalLabel: `${steps.length.toLocaleString()} steps`,
    tokenTotalLabel: `${fmtTokens(totalTokens)} tokens`,
    emptyNote: emptyCount
      ? `${emptyCount.toLocaleString()} empty ${
          emptyCount === 1 ? "step" : "steps"
        } excluded — no message, reasoning, tool call, or output`
      : null,
  };
}

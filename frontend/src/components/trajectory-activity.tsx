"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Activity, Star } from "lucide-react";
import type { TrajectoryStep } from "@/lib/types";
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
import { useTrajectorySummary } from "@/lib/use-trajectory-summary";

interface TrajectoryActivityProps {
  trialId: string;
  steps: TrajectoryStep[];
  apiBaseUrl?: string;
  stepIdToIndex: (stepId: number) => number;
  onStepSelect: (index: number) => void;
}

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

export function TrajectoryActivity({
  trialId,
  steps: allSteps,
  apiBaseUrl = "/api",
  stepIdToIndex,
  onStepSelect,
}: TrajectoryActivityProps) {
  const { data } = useTrajectorySummary(trialId, apiBaseUrl);

  // Empty padding steps are dropped up front, so they cannot be counted in a
  // bar, drawn as a cell, or measured as gap-fill distance further down.
  const steps = allSteps.filter((step) => !isEmptyStep(step));
  const emptyCount = allSteps.length - steps.length;
  const renderableIds = new Set(steps.map((s) => Number(s.step_id)));

  const segments = withOtherSegment(toSegments(data), steps, renderableIds);
  if (!steps.length || segments.length === 0) return null;

  const colorFor = phaseColorVars(segments.map((s) => s.key));
  const labelFor = new Map(segments.map((s) => [s.key, s.label]));
  const owner = segmentOwners(segments, renderableIds);
  // step_id is typed number but arrives as a string from some producers.
  const keyByStep = (stepId: number) => owner.get(Number(stepId))?.key;
  const highlightIds = new Set((data?.highlights ?? []).map((h) => h.step_id));

  const durations = stepDurationsMs(steps);
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

  const sections = [
    {
      name: "Steps",
      totalLabel: steps.length.toLocaleString(),
      value: (m: MetricValues) => m.stepCount,
      fmt: (n: number) => n.toLocaleString(),
    },
    {
      // Sits beside Steps on purpose: a component with few steps but heavy
      // tokens (a big file read) reads very differently from a long cheap one.
      name: "Tokens",
      totalLabel: fmtTokens(totalTokens),
      value: (m: MetricValues) => m.tokenCount,
      fmt: fmtTokens,
      hideWhenEmpty: true,
    },
    {
      name: "Tool calls",
      totalLabel: totalTools.toLocaleString(),
      value: (m: MetricValues) => m.toolCount,
      fmt: (n: number) => n.toLocaleString(),
    },
    {
      name: "Time",
      totalLabel: totalMs > 0 ? fmtDurationMs(totalMs) : "—",
      value: (m: MetricValues) => m.durationMs,
      fmt: (n: number) => (n > 0 ? fmtDurationMs(n) : "—"),
      hideWhenEmpty: true,
    },
  ];

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
  const runs: { key: string | undefined; indexes: number[] }[] = [];
  steps.forEach((step, index) => {
    const key = keyByStep(step.step_id);
    const last = runs[runs.length - 1];
    if (last && last.key === key) last.indexes.push(index);
    else runs.push({ key, indexes: [index] });
  });
  const stepRunWeight = (run: { indexes: number[] }) => run.indexes.length;

  // The token band re-lays the same runs against token share instead of time,
  // so a component that is narrow above and wide here spent few steps and many
  // tokens. Equal widths when no step reports tokens, matching widthPct.
  const tokenPct = (i: number) =>
    totalTokens > 0
      ? ((tokens[i] ?? 0) / totalTokens) * 100
      : 100 / steps.length;
  const tokenRunWeight = (run: { indexes: number[] }) =>
    run.indexes.reduce((sum, i) => sum + tokenPct(i), 0);

  const select = (stepId: number) => {
    const idx = stepIdToIndex(stepId);
    if (idx >= 0) onStepSelect(idx);
  };

  const instanceTitle = (instance: InstanceStat) =>
    [
      instance.label,
      instance.rangeLabel,
      `${instance.toolCount} ${instance.toolCount === 1 ? "tool" : "tools"}`,
      instance.durationMs > 0 ? fmtDurationMs(instance.durationMs) : "—",
    ].join(" · ");

  return (
    <Card className="my-3">
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-sm font-medium">
          <Activity className="h-4 w-4" />
          Activity
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* one mini bar chart per metric; rows double as the timeline legend */}
        {sections.map((section) => {
          // Longest kind fills the track; others are proportional to it.
          const denom = Math.max(
            0,
            ...kinds.map((kind) => section.value(kind))
          );
          // Nothing to plot: codex trajectories carry no timestamps, and some
          // producers report no per-step tokens.
          if (section.hideWhenEmpty && denom <= 0) return null;
          // Largest bar first; stable sort keeps first-appearance order on ties.
          const ranked = [...kinds].sort(
            (a, b) => section.value(b) - section.value(a)
          );
          return (
            <div key={section.name}>
              <div className="flex items-baseline justify-between">
                <span className="text-xs font-medium">{section.name}</span>
                <span className="text-muted-foreground font-mono text-xs">
                  {section.totalLabel} total
                </span>
              </div>
              <div className="mt-1.5 space-y-1">
                {ranked.map((kind) => (
                  <div key={kind.key} className="flex items-center gap-2">
                    <span
                      className="flex w-36 shrink-0 items-center gap-1.5 text-xs"
                      title={kind.label}
                    >
                      <span
                        className="h-3 w-1 shrink-0 rounded-sm"
                        style={{
                          background:
                            colorFor.get(kind.key) ?? "var(--phase-other)",
                        }}
                      />
                      <span className="truncate">{kind.label}</span>
                    </span>
                    {/* gap between segments = the split between instances */}
                    <div className="bg-muted/40 flex h-2 flex-1 gap-0.5 overflow-hidden rounded-sm">
                      {denom > 0 &&
                        kind.instances.map((instance, i) => {
                          const pct = Math.min(
                            100,
                            (section.value(instance) / denom) * 100
                          );
                          if (pct <= 0) return null;
                          return (
                            <button
                              key={`${instance.firstStepId}-${i}`}
                              type="button"
                              title={instanceTitle(instance)}
                              onClick={() => select(instance.firstStepId)}
                              style={{
                                width: `${pct}%`,
                                background:
                                  colorFor.get(kind.key) ??
                                  "var(--phase-other)",
                              }}
                              className="h-full min-w-[3px] shrink-0 rounded-sm transition hover:brightness-110"
                            />
                          );
                        })}
                    </div>
                    <span className="text-muted-foreground w-16 shrink-0 text-right font-mono text-xs">
                      {section.fmt(section.value(kind))}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          );
        })}

        {/* timeline: the same runs measured two ways */}
        <div className="overflow-x-auto">
          <div className="min-w-[560px]">
            <div className="flex items-baseline justify-between">
              <span className="text-xs font-medium">Timeline · by steps</span>
              <span className="text-muted-foreground font-mono text-xs">
                {steps.length.toLocaleString()} steps
              </span>
            </div>
            <div className="mt-1.5 flex h-10 items-stretch gap-0.5">
              {runs.map((run, r) => (
                <div
                  key={r}
                  className="flex overflow-hidden rounded-sm"
                  style={{ flex: `${stepRunWeight(run)} 1 0` }}
                >
                  {run.indexes.map((i) => {
                    const step = steps[i];
                    return (
                      <button
                        key={step.step_id}
                        type="button"
                        title={`Step ${step.step_id} · ${labelFor.get(run.key ?? "") ?? ""} · ${fmtMs(durations[i])}`}
                        onClick={() => select(step.step_id)}
                        style={{
                          flex: "1 1 0",
                          background:
                            colorFor.get(run.key ?? "") ?? "var(--phase-other)",
                        }}
                        className="focus-visible:outline-ring relative min-w-px outline-offset-2 transition hover:brightness-110 focus-visible:outline focus-visible:outline-2"
                      >
                        {highlightIds.has(step.step_id) && (
                          <Star className="absolute top-0.5 right-0.5 h-3 w-3 fill-white text-white drop-shadow" />
                        )}
                      </button>
                    );
                  })}
                </div>
              ))}
            </div>

            {hasTokens && (
              <>
                {/* Per-step token intensity, laid out against the step band
                    above so the columns line up. Answers "which individual
                    steps were heavy", which the aggregate band cannot. */}
                <div className="mt-1 flex h-3 gap-0.5">
                  {runs.map((run, r) => (
                    <div
                      key={r}
                      className="flex overflow-hidden rounded-sm"
                      style={{ flex: `${stepRunWeight(run)} 1 0` }}
                    >
                      {run.indexes.map((i) => {
                        const step = steps[i];
                        const a =
                          12 + Math.round(((tokens[i] ?? 0) / maxTok) * 88);
                        return (
                          <button
                            key={step.step_id}
                            type="button"
                            title={`Step ${step.step_id} · ${(tokens[i] ?? 0).toLocaleString()} tokens`}
                            onClick={() => select(step.step_id)}
                            style={{
                              flex: "1 1 0",
                              background: `color-mix(in srgb, var(--phase-1) ${a}%, transparent)`,
                            }}
                            className="min-w-px"
                          />
                        );
                      })}
                    </div>
                  ))}
                </div>

                {/* the same runs again, sized by tokens not step count */}
                <div className="mt-3 flex items-baseline justify-between">
                  <span className="text-xs font-medium">
                    Timeline · by tokens
                  </span>
                  <span className="text-muted-foreground font-mono text-xs">
                    {fmtTokens(totalTokens)} tokens
                  </span>
                </div>
                <div className="mt-1.5 flex h-10 gap-0.5">
                  {runs.map((run, r) => (
                    <div
                      key={r}
                      className="flex overflow-hidden rounded-sm"
                      style={{ flex: `${tokenRunWeight(run)} 1 0` }}
                    >
                      {run.indexes.map((i) => {
                        const step = steps[i];
                        return (
                          <button
                            key={step.step_id}
                            type="button"
                            title={`Step ${step.step_id} · ${labelFor.get(run.key ?? "") ?? ""} · ${(tokens[i] ?? 0).toLocaleString()} tokens`}
                            onClick={() => select(step.step_id)}
                            style={{
                              flex: `${tokenPct(i)} 1 0`,
                              background:
                                colorFor.get(run.key ?? "") ??
                                "var(--phase-other)",
                            }}
                            className="min-w-px transition hover:brightness-110"
                          />
                        );
                      })}
                    </div>
                  ))}
                </div>
              </>
            )}
            {hasTokens && (
              <p className="text-muted-foreground mt-2 font-mono text-[10.5px]">
                bars share components and order · thin band = tokens per step,
                darker is more · lower band = width by token volume
              </p>
            )}
          </div>
        </div>

        {/* Without this the totals here silently disagree with the step count
            shown for the same trial everywhere else. */}
        {emptyCount > 0 && (
          <p className="text-muted-foreground font-mono text-[10.5px]">
            {emptyCount.toLocaleString()} empty{" "}
            {emptyCount === 1 ? "step" : "steps"} excluded — no message,
            reasoning, tool call, or output
          </p>
        )}
      </CardContent>
    </Card>
  );
}

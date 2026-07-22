"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Activity, Star } from "lucide-react";
import type { TrajectoryStep } from "@/lib/types";
import {
  fmtDurationMs,
  phaseColorVars,
  stepDurationsMs,
  stepTokens,
} from "@/lib/trajectory-metrics";
import { segmentOwners, toSegments } from "@/lib/trajectory-segments";
import { useTrajectorySummary } from "@/lib/use-trajectory-summary";

interface TrajectoryActivityProps {
  trialId: string;
  steps: TrajectoryStep[];
  apiBaseUrl?: string;
  stepIdToIndex: (stepId: number) => number;
  onStepSelect: (index: number) => void;
}

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
  durationMs: number;
}

interface KindStat {
  key: string;
  label: string;
  instances: InstanceStat[];
  stepCount: number;
  toolCount: number;
  durationMs: number;
}

interface MetricValues {
  stepCount: number;
  toolCount: number;
  durationMs: number;
}

export function TrajectoryActivity({
  trialId,
  steps,
  apiBaseUrl = "/api",
  stepIdToIndex,
  onStepSelect,
}: TrajectoryActivityProps) {
  const { data } = useTrajectorySummary(trialId, apiBaseUrl);

  const segments = toSegments(data);
  if (!steps.length || segments.length === 0) return null;

  const colorFor = phaseColorVars(segments.map((s) => s.key));
  const labelFor = new Map(segments.map((s) => [s.key, s.label]));
  const owner = segmentOwners(segments);
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
  const stepById = new Map(
    steps.map((step, index) => [Number(step.step_id), index])
  );

  // One stat per component instance; v5 fields when present, else derived from
  // the live steps so older summaries render identically.
  const instances: InstanceStat[] = segments.map((segment) => {
    const indexes = segment.stepIds
      .map((id) => stepById.get(Number(id)))
      .filter((index): index is number => index !== undefined);
    const ids = segment.stepIds.map(Number);
    const lo = Math.min(...ids);
    const hi = Math.max(...ids);
    return {
      key: segment.key,
      label: segment.label,
      firstStepId: ids[0],
      rangeLabel: lo === hi ? `step ${lo}` : `steps ${lo}–${hi}`,
      stepCount: indexes.length,
      toolCount:
        segment.toolCount ??
        indexes.reduce(
          (sum, index) => sum + (steps[index].tool_calls?.length ?? 0),
          0
        ),
      durationMs:
        segment.durationMs ??
        indexes.reduce((sum, index) => sum + durations[index], 0),
    };
  });

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
        durationMs: 0,
      };
      kindByKey.set(instance.key, kind);
      kinds.push(kind);
    }
    kind.instances.push(instance);
    kind.stepCount += instance.stepCount;
    kind.toolCount += instance.toolCount;
    kind.durationMs += instance.durationMs;
  }
  for (const kind of kinds) {
    kind.instances.sort((a, b) => a.firstStepId - b.firstStepId);
  }

  const sections = [
    {
      name: "Steps",
      total: steps.length,
      totalLabel: steps.length.toLocaleString(),
      value: (m: MetricValues) => m.stepCount,
      fmt: (n: number) => n.toLocaleString(),
    },
    {
      name: "Tool calls",
      total: totalTools,
      totalLabel: totalTools.toLocaleString(),
      value: (m: MetricValues) => m.toolCount,
      fmt: (n: number) => n.toLocaleString(),
    },
    {
      name: "Time",
      total: totalMs,
      totalLabel: totalMs > 0 ? fmtDurationMs(totalMs) : "—",
      value: (m: MetricValues) => m.durationMs,
      fmt: (n: number) => (n > 0 ? fmtDurationMs(n) : "—"),
    },
  ];

  const widthPct = (i: number) =>
    totalMs > 0
      ? Math.max((durations[i] / totalMs) * 100, 1.5)
      : 100 / steps.length;

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
        {sections.map((section) => (
          <div key={section.name}>
            <div className="flex items-baseline justify-between">
              <span className="text-xs font-medium">{section.name}</span>
              <span className="text-muted-foreground font-mono text-xs">
                {section.totalLabel} total
              </span>
            </div>
            <div className="mt-1.5 space-y-1">
              {kinds.map((kind) => (
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
                    {section.total > 0 &&
                      kind.instances.map((instance, i) => {
                        const pct = Math.min(
                          100,
                          (section.value(instance) / section.total) * 100
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
                                colorFor.get(kind.key) ?? "var(--phase-other)",
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
        ))}

        {/* timeline */}
        <div className="overflow-x-auto">
          <div className="min-w-[560px]">
            <div className="flex h-10 items-stretch gap-0.5">
              {steps.map((s, i) => (
                <button
                  key={s.step_id}
                  type="button"
                  title={`Step ${s.step_id} · ${labelFor.get(keyByStep(s.step_id) ?? "") ?? ""} · ${fmtMs(durations[i])}`}
                  onClick={() => select(s.step_id)}
                  style={{
                    flex: `${widthPct(i)} 1 0`,
                    background:
                      colorFor.get(keyByStep(s.step_id) ?? "") ??
                      "var(--phase-other)",
                  }}
                  className="focus-visible:outline-ring relative min-w-[6px] rounded-sm outline-offset-2 transition hover:brightness-110 focus-visible:outline focus-visible:outline-2"
                >
                  {highlightIds.has(s.step_id) && (
                    <Star className="absolute top-0.5 right-0.5 h-3 w-3 fill-white text-white drop-shadow" />
                  )}
                </button>
              ))}
            </div>

            {/* token heatmap */}
            {hasTokens && (
              <div className="mt-1 flex h-3 gap-0.5">
                {steps.map((s, i) => {
                  const a = 12 + Math.round(((tokens[i] ?? 0) / maxTok) * 88);
                  return (
                    <button
                      key={s.step_id}
                      type="button"
                      title={`Step ${s.step_id} · ${(tokens[i] ?? 0).toLocaleString()} tokens`}
                      onClick={() => select(s.step_id)}
                      style={{
                        flex: `${widthPct(i)} 1 0`,
                        background: `color-mix(in srgb, var(--phase-1) ${a}%, transparent)`,
                      }}
                      className="min-w-[3px] rounded-sm"
                    />
                  );
                })}
              </div>
            )}
            {hasTokens && (
              <p className="text-muted-foreground mt-2 font-mono text-[10.5px]">
                token volume per step — darker = more tokens
              </p>
            )}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

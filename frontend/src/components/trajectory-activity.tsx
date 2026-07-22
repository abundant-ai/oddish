"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Activity, Star } from "lucide-react";
import type { TrajectoryStep } from "@/lib/types";
import {
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
  const componentStats = segments.map((segment) => {
    const indexes = segment.stepIds
      .map((id) => stepById.get(Number(id)))
      .filter((index): index is number => index !== undefined);
    return {
      ...segment,
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

  const widthPct = (i: number) =>
    totalMs > 0
      ? Math.max((durations[i] / totalMs) * 100, 1.5)
      : 100 / steps.length;

  const select = (stepId: number) => {
    const idx = stepIdToIndex(stepId);
    if (idx >= 0) onStepSelect(idx);
  };

  return (
    <Card className="my-3">
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-sm font-medium">
          <Activity className="h-4 w-4" />
          Activity
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-3 gap-2">
          <Metric label="Steps" value={steps.length.toLocaleString()} />
          <Metric label="Tool calls" value={totalTools.toLocaleString()} />
          <Metric label="Elapsed" value={totalMs > 0 ? fmtMs(totalMs) : "—"} />
        </div>

        <div className="overflow-hidden rounded-md border">
          {componentStats.map((component, index) => (
            <div
              key={`${component.key}-${index}`}
              className="grid grid-cols-[minmax(0,1fr)_auto_auto_auto] items-center gap-3 border-b px-3 py-2 text-xs last:border-b-0"
            >
              <span className="flex min-w-0 items-center gap-2 font-medium">
                <span
                  className="h-3 w-1 shrink-0 rounded-sm"
                  style={{
                    background:
                      colorFor.get(component.key) ?? "var(--phase-other)",
                  }}
                />
                <span className="truncate">{component.label}</span>
              </span>
              <span className="text-muted-foreground font-mono">
                {component.stepCount}{" "}
                {component.stepCount === 1 ? "step" : "steps"}
              </span>
              <span className="text-muted-foreground font-mono">
                {component.toolCount}{" "}
                {component.toolCount === 1 ? "tool" : "tools"}
              </span>
              <span className="text-muted-foreground w-14 text-right font-mono">
                {component.durationMs > 0 ? fmtMs(component.durationMs) : "—"}
              </span>
            </div>
          ))}
        </div>

        {/* legend */}
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          {[...colorFor.entries()].map(([key, color]) => (
            <span
              key={key}
              className="text-muted-foreground flex items-center gap-1.5 font-mono text-xs"
            >
              <span
                className="h-2.5 w-2.5 rounded-sm"
                style={{ background: color }}
              />
              {labelFor.get(key) ?? key}
            </span>
          ))}
        </div>

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

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-muted/20 rounded-md border px-3 py-2">
      <div className="font-mono text-base font-semibold tabular-nums">
        {value}
      </div>
      <div className="text-muted-foreground text-[11px]">{label}</div>
    </div>
  );
}

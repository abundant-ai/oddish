"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Activity, Star } from "lucide-react";
import type { TrajectoryStep } from "@/lib/types";
import { phaseColorVars, stepDurationsMs, stepTokens } from "@/lib/trajectory-metrics";
import { toSegments } from "@/lib/trajectory-segments";
import { useTrajectorySummary } from "@/lib/use-trajectory-summary";

interface TrajectoryActivityProps {
  trialId: string;
  steps: TrajectoryStep[];
  apiBaseUrl?: string;
  stepIdToIndex: (stepId: number) => number;
  onStepSelect: (index: number) => void;
}

const fmtMs = (ms: number) =>
  ms >= 1000 ? `${(ms / 1000).toFixed(ms >= 10000 ? 0 : 1)}s` : `${Math.round(ms)}ms`;

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
  const keyByStep = new Map<number, string>();
  segments.forEach((s) => s.stepIds.forEach((id) => keyByStep.set(id, s.key)));
  const highlightIds = new Set((data?.highlights ?? []).map((h) => h.step_id));

  const durations = stepDurationsMs(steps);
  const totalMs = durations.reduce((a, b) => a + b, 0);
  const tokens = steps.map(stepTokens);
  const maxTok = Math.max(0, ...tokens.map((t) => t ?? 0));
  const hasTokens = maxTok > 0;

  const widthPct = (i: number) =>
    totalMs > 0 ? Math.max((durations[i] / totalMs) * 100, 1.5) : 100 / steps.length;

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
        {/* legend */}
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          {[...colorFor.entries()].map(([key, color]) => (
            <span
              key={key}
              className="flex items-center gap-1.5 font-mono text-xs text-muted-foreground"
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
                  title={`Step ${s.step_id} · ${labelFor.get(keyByStep.get(s.step_id) ?? "") ?? ""} · ${fmtMs(durations[i])}`}
                  onClick={() => select(s.step_id)}
                  style={{
                    flex: `${widthPct(i)} 1 0`,
                    background: colorFor.get(keyByStep.get(s.step_id) ?? "") ?? "var(--phase-other)",
                  }}
                  className="relative min-w-[6px] rounded-sm outline-offset-2 transition hover:brightness-110 focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring"
                >
                  {highlightIds.has(s.step_id) && (
                    <Star className="absolute right-0.5 top-0.5 h-3 w-3 fill-white text-white drop-shadow" />
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
              <p className="mt-2 font-mono text-[10.5px] text-muted-foreground">
                token volume per step — darker = more tokens
              </p>
            )}
          </div>
        </div>

        {/* steps by component */}
        <div className="space-y-4 pt-1">
          {segments.map((segment, pi) => {
            const color = colorFor.get(segment.key) ?? "var(--phase-other)";
            const ids = segment.stepIds;
            const range =
              ids.length === 1
                ? `step ${ids[0]}`
                : `steps ${ids[0]}–${ids[ids.length - 1]}`;
            return (
              <div key={`${segment.key}-${pi}`}>
                <div className="flex items-center gap-2 border-b pb-1.5">
                  <span
                    className="h-4 w-1 rounded-sm"
                    style={{ background: color }}
                  />
                  <span className="text-sm font-semibold">{segment.label}</span>
                  <span className="ml-auto font-mono text-xs text-muted-foreground">
                    {range}
                  </span>
                </div>
                {segment.gist && (
                  <p className="pt-1.5 text-xs text-muted-foreground">{segment.gist}</p>
                )}
                <div className="grid gap-0.5 pt-1">
                  {ids.map((id) => {
                    const idx = stepIdToIndex(id);
                    const step = idx >= 0 ? steps[idx] : undefined;
                    return (
                      <button
                        key={id}
                        type="button"
                        disabled={idx < 0}
                        onClick={() => select(id)}
                        style={{ borderColor: color }}
                        className="flex items-center gap-2 rounded-md border-l-2 border-transparent px-2 py-1.5 text-left text-sm hover:bg-muted disabled:opacity-50"
                      >
                        <span className="font-mono text-xs text-muted-foreground">
                          {String(id).padStart(2, "0")}
                        </span>
                        <span className="flex-1 truncate">
                          {step ? summarizeStep(step) : `Step ${id}`}
                        </span>
                        {highlightIds.has(id) && (
                          <Star className="h-3 w-3 shrink-0 fill-current text-yellow-500" />
                        )}
                      </button>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}

/** One-line label for a step in the component list. */
function summarizeStep(step: TrajectoryStep): string {
  if (step.tool_calls && step.tool_calls.length > 0) {
    const names = step.tool_calls
      .map((c) => c.function_name)
      .filter(Boolean)
      .join(", ");
    if (names) return names;
  }
  if (typeof step.message === "string" && step.message.trim()) {
    return step.message.trim().slice(0, 80);
  }
  return step.source;
}

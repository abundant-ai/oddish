"use client";

import { memo, useCallback, useMemo } from "react";
import {
  PolarAngleAxis,
  PolarGrid,
  PolarRadiusAxis,
  Radar,
  RadarChart,
  ResponsiveContainer,
  Tooltip,
} from "recharts";
import type { TooltipContentProps } from "recharts";
import type { Task } from "@/lib/types";
import { buildAgentParetoPoints } from "@/lib/pareto";
import {
  costEstimateMarks,
  formatCostUsd,
  formatDurationSec,
  formatTokenCount,
} from "@/lib/format";
import type { ExperimentAgentSummary } from "@/lib/experiment-agent-grouping";
import { useElementSize } from "@/lib/use-element-size";
import { AGENT_COLORS } from "./pass-at-k-graph";
import { AgentLegend } from "@/components/agent-legend";

interface AgentStatRadarProps {
  tasks: Task[];
  agentSummaries: ExperimentAgentSummary[];
  hiddenAgents: Set<string>;
  onToggleAgent: (agent: string) => void;
  hoverAgent?: string | null;
  onHoverAgent?: (key: string | null) => void;
}

type TooltipValue = number | string | ReadonlyArray<number | string>;
type TooltipName = number | string;

// The radar's lower-is-better axes: shown as efficiency relative to the best
// visible agent (min/value, outer edge = best). Score stays absolute pass@1.
const EFFICIENCY_AXES = [
  { axis: "cheap", metric: "cost", format: formatCostUsd },
  { axis: "light", metric: "tokens", format: formatTokenCount },
  { axis: "fast", metric: "time", format: formatDurationSec },
  {
    axis: "lean",
    metric: "steps",
    format: (v: number) => `${Math.round(v)} steps`,
  },
] as const;

type AxisRow = { axis: string } & Record<string, number | string>;

export const AgentStatRadar = memo(function AgentStatRadar({
  tasks,
  agentSummaries,
  hiddenAgents,
  onToggleAgent,
  hoverAgent,
  onHoverAgent,
}: AgentStatRadarProps) {
  const { ref: chartContainerRef, size: chartSize } =
    useElementSize<HTMLDivElement>();

  const { axisRows, visibleKeys, rawByAxis, agentColorByKey, agentLabelByKey } =
    useMemo(() => {
      const colorMap: Record<string, string> = {};
      const labelMap: Record<string, string> = {};
      for (let i = 0; i < agentSummaries.length; i++) {
        colorMap[agentSummaries[i].key] = AGENT_COLORS[i % AGENT_COLORS.length];
        labelMap[agentSummaries[i].key] = agentSummaries[i].label;
      }

      const points = buildAgentParetoPoints(tasks, agentSummaries).filter(
        (point) => !hiddenAgents.has(point.key)
      );

      // Per-metric best among the shown agents; axes nobody reports are
      // dropped so every polygon spans the same spokes. Hiding an outlier
      // re-normalizes the rest, like the Pareto frontier does.
      const minByMetric = new Map<string, number>();
      for (const { metric } of EFFICIENCY_AXES) {
        const values = points
          .map((point) => point.metrics[metric]?.value)
          .filter((value): value is number => value != null && value > 0);
        if (values.length > 0) minByMetric.set(metric, Math.min(...values));
      }

      const raw = new Map<string, Map<string, string>>();
      const scoreRow: AxisRow = { axis: "score" };
      const scoreRaw = new Map<string, string>();
      for (const point of points) {
        scoreRow[point.key] = point.score;
        scoreRaw.set(point.key, `${(point.score * 100).toFixed(1)}%`);
      }
      raw.set("score", scoreRaw);

      const rows: AxisRow[] = [scoreRow];
      for (const { axis, metric, format } of EFFICIENCY_AXES) {
        const min = minByMetric.get(metric);
        if (min == null) continue;
        const row: AxisRow = { axis };
        const axisRaw = new Map<string, string>();
        for (const point of points) {
          const value = point.metrics[metric]?.value;
          // Unreported reads as 0 (tooltip says so); zero-cost is maximal
          // efficiency, not a division blowup.
          row[point.key] =
            value == null ? 0 : value <= 0 ? 1 : Math.min(1, min / value);
          const marks =
            metric === "cost"
              ? costEstimateMarks(point.costHasEstimated, point.costHasNative)
              : { prefix: "", suffix: "" };
          axisRaw.set(
            point.key,
            value == null
              ? "not reported"
              : `${marks.prefix}${format(value)}${marks.suffix}`
          );
        }
        raw.set(axis, axisRaw);
        rows.push(row);
      }

      return {
        axisRows: rows,
        visibleKeys: points.map((point) => point.key),
        rawByAxis: raw,
        agentColorByKey: colorMap,
        agentLabelByKey: labelMap,
      };
    }, [tasks, agentSummaries, hiddenAgents]);

  const renderTooltip = useCallback(
    (props: TooltipContentProps<TooltipValue, TooltipName>) => {
      const { active, payload, label } = props;
      if (!active || !payload || payload.length === 0) return null;
      const axisRaw = rawByAxis.get(String(label));

      const sorted = [...payload]
        .filter((entry) => typeof entry.value === "number")
        .sort((a, b) => (Number(b.value) || 0) - (Number(a.value) || 0));

      return (
        <div
          style={{
            backgroundColor: "var(--paper-surface)",
            border: "1px solid var(--paper-line)",
            borderRadius: "8px",
            padding: "8px 12px",
            fontSize: "11.5px",
            fontFamily: "var(--font-geist-mono), ui-monospace, monospace",
            boxShadow: "0 4px 14px rgba(0,0,0,0.08)",
            color: "var(--paper-ink)",
          }}
        >
          <div
            style={{
              marginBottom: "4px",
              fontWeight: 600,
              color: "var(--paper-ink-2)",
            }}
          >
            {label}
          </div>
          {sorted.map((entry) => {
            const key = entry.dataKey as string;
            const isHovered = hoverAgent != null && hoverAgent === key;
            return (
              <div
                key={key}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "6px",
                  padding: "1px 0",
                  whiteSpace: "nowrap",
                  fontWeight: isHovered ? 600 : 400,
                }}
              >
                <span
                  style={{
                    width: "8px",
                    height: "8px",
                    borderRadius: "2px",
                    backgroundColor:
                      agentColorByKey[key] ?? "var(--paper-ink-3)",
                    flexShrink: 0,
                  }}
                />
                <span style={{ color: "var(--paper-ink-2)" }}>
                  {agentLabelByKey[key] ?? key}
                </span>
                <span
                  style={{
                    marginLeft: "auto",
                    paddingLeft: "12px",
                    fontWeight: 500,
                    color: "var(--paper-ink)",
                  }}
                >
                  {axisRaw?.get(key) ?? ""}
                </span>
              </div>
            );
          })}
        </div>
      );
    },
    [rawByAxis, agentColorByKey, agentLabelByKey, hoverAgent]
  );

  if (visibleKeys.length === 0 || axisRows.length < 3) {
    return null;
  }

  return (
    <div className="flex h-full min-w-0 flex-col rounded-[10px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-4 py-3">
      <div className="mb-2 flex items-baseline justify-between gap-3">
        <h3 className="font-display text-[15px] font-medium tracking-[-0.01em] text-[color:var(--paper-ink)]">
          Stat polygon
        </h3>
        <span
          className="font-mono text-[10.5px] text-[color:var(--paper-ink-3)]"
          title="Score is pass@1; cheap/light/fast/lean are cost, tokens, time, and steps relative to the best shown agent"
        >
          outer edge = best shown
        </span>
      </div>

      <div ref={chartContainerRef} className="h-52 min-w-0">
        {chartSize.width > 0 && chartSize.height > 0 ? (
          <ResponsiveContainer
            width={chartSize.width}
            height={chartSize.height}
          >
            <RadarChart data={axisRows} cx="50%" cy="50%" outerRadius="76%">
              <PolarGrid stroke="var(--paper-line-2)" />
              <PolarAngleAxis
                dataKey="axis"
                tick={{
                  fontSize: 10,
                  fill: "var(--paper-ink-2)",
                  fontFamily: "var(--font-geist-mono), ui-monospace, monospace",
                }}
              />
              <PolarRadiusAxis domain={[0, 1]} tick={false} axisLine={false} />
              <Tooltip
                content={renderTooltip}
                wrapperStyle={{ zIndex: 10, outline: "none" }}
              />
              {visibleKeys.map((key) => {
                const color = agentColorByKey[key] ?? AGENT_COLORS[0];
                const isHovered = hoverAgent === key;
                const isDimmed = hoverAgent != null && hoverAgent !== key;
                return (
                  <Radar
                    key={key}
                    dataKey={key}
                    stroke={color}
                    strokeWidth={isHovered ? 2.6 : 1.8}
                    strokeOpacity={isDimmed ? 0.2 : 1}
                    fill={color}
                    fillOpacity={isHovered ? 0.22 : isDimmed ? 0.02 : 0.09}
                    dot={{ r: isHovered ? 3 : 2, fill: color, strokeWidth: 0 }}
                    isAnimationActive={false}
                    onMouseEnter={() => onHoverAgent?.(key)}
                    onMouseLeave={() => onHoverAgent?.(null)}
                    style={{ cursor: "pointer" }}
                  />
                );
              })}
            </RadarChart>
          </ResponsiveContainer>
        ) : null}
      </div>

      <AgentLegend
        items={agentSummaries.map((summary, idx) => ({
          key: summary.key,
          label: summary.label,
          color:
            agentColorByKey[summary.key] ??
            AGENT_COLORS[idx % AGENT_COLORS.length],
          queueKey: summary.queueKey,
          model: summary.model,
          agent: summary.agent,
        }))}
        hiddenKeys={hiddenAgents}
        onToggle={onToggleAgent}
        hoverKey={hoverAgent ?? null}
        onHover={onHoverAgent}
      />
    </div>
  );
});

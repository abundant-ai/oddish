"use client";

import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CartesianGrid,
  ComposedChart,
  LabelList,
  Line,
  ResponsiveContainer,
  Scatter,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { TooltipContentProps } from "recharts";
import type { Task } from "@/lib/types";
import {
  buildAgentParetoPoints,
  paretoFrontier,
  PARETO_METRICS,
  type AgentParetoPoint,
  type ParetoMetric,
} from "@/lib/pareto";
import {
  costEstimateMarks,
  formatCostUsd,
  formatDurationSec,
  formatTokenCount,
} from "@/lib/format";
import type { AgentSummary } from "./experiment-trials-table";
import { AGENT_COLORS } from "./pass-at-k-graph";
import { AgentLegend } from "@/components/agent-legend";

interface CostParetoGraphProps {
  tasks: Task[];
  agentSummaries: AgentSummary[];
  hiddenAgents: Set<string>;
  onToggleAgent: (agent: string) => void;
  hoverAgent?: string | null;
  onHoverAgent?: (key: string | null) => void;
}

type ChartDatum = {
  key: string;
  label: string;
  x: number;
  y: number;
  taskCount: number;
  trialCount: number;
  /** Trials that reported the active metric (the x mean's denominator). */
  metricTrialCount: number;
  /** "~" all-estimated / "*" mixed, cost metric only. */
  estimatePrefix: string;
  estimateSuffix: string;
};

type TooltipValue = number | string | ReadonlyArray<number | string>;
type TooltipName = number | string;

const METRIC_LABEL: Record<ParetoMetric, string> = {
  cost: "cost",
  tokens: "tokens",
  time: "time",
};

const METRIC_AXIS_LABEL: Record<ParetoMetric, string> = {
  cost: "avg $ / trial",
  tokens: "avg tokens / trial",
  time: "avg time / trial",
};

function formatMetricTick(metric: ParetoMetric, value: number): string {
  if (metric === "cost") {
    if (value === 0) return "$0";
    if (value >= 100) return `$${Math.round(value)}`;
    if (value >= 10) return `$${value.toFixed(0)}`;
    if (value >= 1) return `$${value.toFixed(1)}`;
    return `$${value.toFixed(2)}`;
  }
  if (metric === "tokens") {
    if (value === 0) return "0";
    if (value >= 1e9) return `${(value / 1e9).toFixed(1)}B`;
    if (value >= 1e6) return `${(value / 1e6).toFixed(1)}M`;
    if (value >= 1e3) return `${(value / 1e3).toFixed(0)}k`;
    return `${Math.round(value)}`;
  }
  if (value === 0) return "0s";
  if (value < 60) return `${Math.round(value)}s`;
  if (value < 3600) return `${Math.round(value / 60)}m`;
  return `${(value / 3600).toFixed(1)}h`;
}

function formatMetricValue(metric: ParetoMetric, value: number): string {
  if (metric === "cost") {
    // formatCostUsd floors at $0.00; keep sub-cent per-trial means readable.
    return value > 0 && value < 0.005
      ? `$${value.toFixed(4)}`
      : formatCostUsd(value);
  }
  if (metric === "tokens") return formatTokenCount(value);
  return formatDurationSec(value);
}

function truncateLabel(value: unknown): string {
  const text = String(value ?? "");
  return text.length > 18 ? `${text.slice(0, 17)}…` : text;
}

function buildDatum(
  point: AgentParetoPoint,
  metric: ParetoMetric,
  label: string
): ChartDatum | null {
  const aggregate = point.metrics[metric];
  if (aggregate == null) return null;
  const marks =
    metric === "cost"
      ? costEstimateMarks(point.costHasEstimated, point.costHasNative)
      : { prefix: "", suffix: "" };
  return {
    key: point.key,
    label,
    x: aggregate.perTrial,
    y: point.score,
    taskCount: point.taskCount,
    trialCount: point.trialCount,
    metricTrialCount: aggregate.trialCount,
    estimatePrefix: marks.prefix,
    estimateSuffix: marks.suffix,
  };
}

export const CostParetoGraph = memo(function CostParetoGraph({
  tasks,
  agentSummaries,
  hiddenAgents,
  onToggleAgent,
  hoverAgent,
  onHoverAgent,
}: CostParetoGraphProps) {
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const [chartSize, setChartSize] = useState({ width: 0, height: 0 });
  const [requestedMetric, setRequestedMetric] = useState<ParetoMetric>("cost");

  useEffect(() => {
    const element = chartContainerRef.current;
    if (!element) return;

    const updateSize = () => {
      const rect = element.getBoundingClientRect();
      setChartSize({
        width: Math.max(0, Math.floor(rect.width)),
        height: Math.max(0, Math.floor(rect.height)),
      });
    };

    updateSize();
    const observer = new ResizeObserver(updateSize);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const { pointByKey, availableMetrics, agentColorByKey, agentLabelByKey } =
    useMemo(() => {
      const points = buildAgentParetoPoints(tasks, agentSummaries);
      const byKey = new Map(points.map((point) => [point.key, point]));
      const available = PARETO_METRICS.filter((metric) =>
        points.some((point) => point.metrics[metric] != null)
      );

      const colorMap: Record<string, string> = {};
      const labelMap: Record<string, string> = {};
      for (let i = 0; i < agentSummaries.length; i++) {
        colorMap[agentSummaries[i].key] = AGENT_COLORS[i % AGENT_COLORS.length];
        labelMap[agentSummaries[i].key] = agentSummaries[i].label;
      }

      return {
        pointByKey: byKey,
        availableMetrics: available,
        agentColorByKey: colorMap,
        agentLabelByKey: labelMap,
      };
    }, [tasks, agentSummaries]);

  const metric = availableMetrics.includes(requestedMetric)
    ? requestedMetric
    : availableMetrics[0];

  const { visibleData, frontierData } = useMemo(() => {
    if (metric == null) {
      return {
        visibleData: [] as ChartDatum[],
        frontierData: [] as ChartDatum[],
      };
    }
    const data: ChartDatum[] = [];
    for (const summary of agentSummaries) {
      if (hiddenAgents.has(summary.key)) continue;
      const point = pointByKey.get(summary.key);
      if (!point) continue;
      const datum = buildDatum(point, metric, agentLabelByKey[summary.key]);
      if (datum) data.push(datum);
    }
    return {
      visibleData: data,
      // Frontier over the agents currently shown: hiding one re-derives the
      // best achievable score per budget among the rest.
      frontierData: paretoFrontier(
        data,
        (d) => d.x,
        (d) => d.y
      ),
    };
  }, [metric, agentSummaries, hiddenAgents, pointByKey, agentLabelByKey]);

  const frontierKeys = useMemo(
    () => new Set(frontierData.map((datum) => datum.key)),
    [frontierData]
  );

  const renderTooltip = useCallback(
    (props: TooltipContentProps<TooltipValue, TooltipName>) => {
      const { active, payload } = props;
      if (!active || !payload || payload.length === 0 || metric == null) {
        return null;
      }
      const datum = payload[0]?.payload as ChartDatum | undefined;
      if (!datum) return null;

      const metricCoverage =
        datum.metricTrialCount < datum.trialCount
          ? ` (${datum.metricTrialCount}/${datum.trialCount} trials reported)`
          : "";

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
              display: "flex",
              alignItems: "center",
              gap: "6px",
              marginBottom: "4px",
              fontWeight: 600,
              color: "var(--paper-ink-2)",
            }}
          >
            <span
              style={{
                width: "8px",
                height: "8px",
                borderRadius: "2px",
                backgroundColor:
                  agentColorByKey[datum.key] ?? "var(--paper-ink-3)",
                flexShrink: 0,
              }}
            />
            {datum.label}
            {frontierKeys.has(datum.key) && (
              <span style={{ fontWeight: 400, color: "var(--paper-ink-3)" }}>
                · on frontier
              </span>
            )}
          </div>
          <div style={{ padding: "1px 0" }}>
            <span style={{ color: "var(--paper-ink-2)" }}>pass@1 </span>
            <span style={{ fontWeight: 500 }}>
              {(datum.y * 100).toFixed(1)}%
            </span>
          </div>
          <div style={{ padding: "1px 0" }}>
            <span style={{ color: "var(--paper-ink-2)" }}>
              {METRIC_AXIS_LABEL[metric]}{" "}
            </span>
            <span style={{ fontWeight: 500 }}>
              {datum.estimatePrefix}
              {formatMetricValue(metric, datum.x)}
              {datum.estimateSuffix}
            </span>
            {(datum.estimatePrefix || datum.estimateSuffix) && (
              <span style={{ color: "var(--paper-ink-3)" }}>
                {" "}
                {datum.estimatePrefix ? "(estimated)" : "(some estimated)"}
              </span>
            )}
          </div>
          <div style={{ padding: "1px 0", color: "var(--paper-ink-3)" }}>
            {datum.taskCount} task{datum.taskCount === 1 ? "" : "s"} ·{" "}
            {datum.trialCount} trial{datum.trialCount === 1 ? "" : "s"}
            {metricCoverage}
          </div>
        </div>
      );
    },
    [metric, agentColorByKey, frontierKeys]
  );

  // Frontier point labels, flipped near the SVG edges so they never clip:
  // right-anchored at the right edge, below the point at the top (where the
  // step line arrives vertically, so below-left stays clear of it).
  const renderFrontierLabel = useCallback(
    (props: { x?: number | string; y?: number | string; value?: unknown }) => {
      const px = Number(props.x);
      const py = Number(props.y);
      if (!Number.isFinite(px) || !Number.isFinite(py)) return null;
      const nearTop = py < 22;
      const nearRight = px > chartSize.width - 80;
      const flip = nearTop || nearRight;
      return (
        <text
          x={flip ? px - 8 : px}
          y={nearTop ? py + 14 : py - 9}
          textAnchor={flip ? "end" : "middle"}
          fontSize={9.5}
          fill="var(--paper-ink-2)"
          fontFamily="var(--font-geist-mono), ui-monospace, monospace"
        >
          {truncateLabel(props.value)}
        </text>
      );
    },
    [chartSize.width]
  );

  if (metric == null || pointByKey.size === 0) {
    return null;
  }

  return (
    <div className="flex h-full min-w-0 flex-col rounded-[10px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-4 py-3">
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h3 className="font-display text-[15px] font-medium tracking-[-0.01em] text-[color:var(--paper-ink)]">
          Pareto frontier
        </h3>
        <div className="flex items-center gap-1">
          {PARETO_METRICS.map((candidate) => {
            const enabled = availableMetrics.includes(candidate);
            const isActive = candidate === metric;
            return (
              <button
                key={candidate}
                type="button"
                onClick={() => enabled && setRequestedMetric(candidate)}
                disabled={!enabled}
                aria-pressed={isActive}
                title={
                  enabled
                    ? `Plot pass@1 against average ${METRIC_LABEL[candidate]} per trial`
                    : `No ${METRIC_LABEL[candidate]} data reported`
                }
                className={`rounded-[5px] border px-2 py-0.5 font-mono text-[10.5px] leading-[1.6] transition-colors select-none ${
                  isActive
                    ? "border-[color:var(--paper-ink)] bg-[color:var(--paper-ink)] text-[color:var(--paper-bg)]"
                    : enabled
                      ? "cursor-pointer border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] text-[color:var(--paper-ink-2)] hover:border-[color:var(--paper-ink-4)] hover:bg-[color:var(--paper-surface-2)]"
                      : "cursor-default border-[color:var(--paper-line-2)] bg-transparent text-[color:var(--paper-ink-4)]"
                }`}
              >
                {METRIC_LABEL[candidate]}
              </button>
            );
          })}
        </div>
      </div>

      <div ref={chartContainerRef} className="h-52 min-w-0">
        {chartSize.width > 0 && chartSize.height > 0 ? (
          <ResponsiveContainer
            width={chartSize.width}
            height={chartSize.height}
          >
            <ComposedChart margin={{ top: 14, right: 16, left: 0, bottom: 5 }}>
              <CartesianGrid
                strokeDasharray="2 4"
                stroke="var(--paper-line-2)"
                vertical={false}
              />
              <XAxis
                type="number"
                dataKey="x"
                domain={[0, "auto"]}
                tickFormatter={(v) => formatMetricTick(metric, Number(v))}
                tick={{
                  fontSize: 10.5,
                  fill: "var(--paper-ink-2)",
                  fontFamily: "var(--font-geist-mono), ui-monospace, monospace",
                }}
                stroke="var(--paper-line)"
                label={{
                  value: METRIC_AXIS_LABEL[metric],
                  position: "insideBottomRight",
                  offset: -5,
                  fontSize: 10,
                  fontStyle: "italic",
                  fill: "var(--paper-ink-3)",
                }}
              />
              <YAxis
                type="number"
                dataKey="y"
                domain={[0, 1]}
                tickFormatter={(v) => `${Math.round(Number(v) * 100)}%`}
                tick={{
                  fontSize: 9.5,
                  fill: "var(--paper-ink-3)",
                  fontFamily: "var(--font-geist-mono), ui-monospace, monospace",
                }}
                stroke="var(--paper-line)"
                width={40}
              />
              <Tooltip
                content={renderTooltip}
                wrapperStyle={{ zIndex: 10, outline: "none" }}
                cursor={{
                  stroke: "var(--paper-ink-4)",
                  strokeWidth: 1,
                  strokeDasharray: "3 3",
                }}
              />
              {/* The frontier itself: a dashed step boundary (not a data
                  series) — as the budget grows, the best score holds flat
                  until the next non-dominated agent. */}
              <Line
                data={frontierData}
                dataKey="y"
                type="stepAfter"
                stroke="var(--paper-ink-3)"
                strokeWidth={1.5}
                strokeDasharray="5 4"
                dot={false}
                activeDot={false}
                isAnimationActive={false}
                tooltipType="none"
              >
                <LabelList dataKey="label" content={renderFrontierLabel} />
              </Line>
              {visibleData.map((datum) => {
                const color = agentColorByKey[datum.key] ?? AGENT_COLORS[0];
                const isHovered = hoverAgent === datum.key;
                const isDimmed = hoverAgent != null && hoverAgent !== datum.key;
                return (
                  <Scatter
                    key={datum.key}
                    data={[datum]}
                    fill={color}
                    fillOpacity={isDimmed ? 0.25 : 1}
                    stroke="var(--paper-surface)"
                    strokeWidth={isHovered ? 2.5 : 2}
                    isAnimationActive={false}
                    onMouseEnter={() => onHoverAgent?.(datum.key)}
                    onMouseLeave={() => onHoverAgent?.(null)}
                    style={{ cursor: "pointer" }}
                  />
                );
              })}
            </ComposedChart>
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

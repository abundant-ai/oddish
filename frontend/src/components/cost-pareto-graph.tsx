"use client";

import { memo, useCallback, useMemo, useState } from "react";
import { useUser } from "@clerk/nextjs";
import {
  CartesianGrid,
  Cell,
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
import type { ExperimentAgentSummary } from "@/lib/experiment-agent-grouping";
import { useElementSize } from "@/lib/use-element-size";
import { AGENT_COLORS } from "./pass-at-k-graph";
import { AgentLegend } from "@/components/agent-legend";

interface CostParetoGraphProps {
  tasks: Task[];
  agentSummaries: ExperimentAgentSummary[];
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

// Feature flag: the Pareto frontier card is visible only to these accounts
// while it bakes (hardcoded-flag convention, like trial-detail-panel's
// re-run-analysis button). Delete the gate in the component to launch it
// for everyone, including public share views.
const PARETO_GRAPH_USER_ALLOWLIST = new Set(["meji@abundant.ai"]);

function formatCount(value: number): string {
  if (value === 0) return "0";
  if (value >= 1e9) return `${(value / 1e9).toFixed(1)}B`;
  if (value >= 1e6) return `${(value / 1e6).toFixed(1)}M`;
  if (value >= 1e3) return `${(value / 1e3).toFixed(0)}k`;
  return `${Math.round(value)}`;
}

function formatMeanCount(value: number, unit: string): string {
  return `${value < 10 ? value.toFixed(1) : formatCount(value)} ${unit}`;
}

function formatDollarTick(v: number): string {
  if (v === 0) return "$0";
  if (v >= 10) return `$${v.toFixed(0)}`;
  if (v >= 1) return `$${v.toFixed(1)}`;
  return `$${v.toFixed(2)}`;
}

// formatCostUsd floors at $0.00; keep sub-cent per-trial means readable.
function formatDollarValue(v: number): string {
  return v > 0 && v < 0.005 ? `$${v.toFixed(4)}` : formatCostUsd(v);
}

// One display entry per metric in lib/pareto.ts. The Record type makes every
// field compiler-enforced, so a newly added metric cannot silently fall
// through to another metric's labels or formatting.
const METRIC_DEFS: Record<
  ParetoMetric,
  {
    label: string;
    axisLabel: string;
    formatTick: (value: number) => string;
    formatValue: (value: number) => string;
  }
> = {
  cost: {
    label: "cost",
    axisLabel: "avg $ / trial",
    formatTick: formatDollarTick,
    formatValue: formatDollarValue,
  },
  costPerSuccess: {
    label: "cost / success",
    axisLabel: "avg $ / success",
    formatTick: formatDollarTick,
    formatValue: formatDollarValue,
  },
  tokens: {
    label: "tokens",
    axisLabel: "avg tokens / trial",
    formatTick: formatCount,
    formatValue: formatTokenCount,
  },
  time: {
    label: "time",
    axisLabel: "avg time / trial",
    formatTick: (v) =>
      v === 0
        ? "0s"
        : v < 60
          ? `${Math.round(v)}s`
          : v < 3600
            ? `${Math.round(v / 60)}m`
            : `${(v / 3600).toFixed(1)}h`,
    formatValue: formatDurationSec,
  },
  steps: {
    label: "steps",
    axisLabel: "avg steps / trial",
    formatTick: formatCount,
    formatValue: (v) => formatMeanCount(v, "steps"),
  },
  tools: {
    label: "tool calls",
    axisLabel: "avg tool calls / trial",
    formatTick: formatCount,
    formatValue: (v) => formatMeanCount(v, "tool calls"),
  },
};

function truncateLabel(value: unknown): string {
  const text = String(value ?? "");
  return text.length > 18 ? `${text.slice(0, 17)}…` : text;
}

function buildDatum(
  point: AgentParetoPoint,
  metric: ParetoMetric
): ChartDatum | null {
  const aggregate = point.metrics[metric];
  if (aggregate == null) return null;
  const marks =
    metric === "cost" || metric === "costPerSuccess"
      ? costEstimateMarks(point.costHasEstimated, point.costHasNative)
      : { prefix: "", suffix: "" };
  return {
    key: point.key,
    label: point.label,
    x: aggregate.value,
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
  const { user } = useUser();
  const { ref: chartContainerRef, size: chartSize } =
    useElementSize<HTMLDivElement>();
  const [requestedMetric, setRequestedMetric] = useState<ParetoMetric>("cost");

  const { points, availableMetrics, agentColorByKey } = useMemo(() => {
    const colorMap: Record<string, string> = {};
    for (let i = 0; i < agentSummaries.length; i++) {
      colorMap[agentSummaries[i].key] = AGENT_COLORS[i % AGENT_COLORS.length];
    }
    const builtPoints = buildAgentParetoPoints(tasks, agentSummaries);
    return {
      points: builtPoints,
      availableMetrics: PARETO_METRICS.filter((metric) =>
        builtPoints.some((point) => point.metrics[metric] != null)
      ),
      agentColorByKey: colorMap,
    };
  }, [tasks, agentSummaries]);

  const metric = availableMetrics.includes(requestedMetric)
    ? requestedMetric
    : availableMetrics[0];

  const { visibleData, frontierData } = useMemo(() => {
    const data =
      metric == null
        ? []
        : points
            .filter((point) => !hiddenAgents.has(point.key))
            .map((point) => buildDatum(point, metric))
            .filter((datum): datum is ChartDatum => datum != null);
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
  }, [metric, points, hiddenAgents]);

  const frontierKeys = useMemo(
    () => new Set(frontierData.map((datum) => datum.key)),
    [frontierData]
  );

  const legendItems = useMemo(
    () =>
      agentSummaries.map((summary) => ({
        key: summary.key,
        label: summary.label,
        color: agentColorByKey[summary.key] ?? AGENT_COLORS[0],
        queueKey: summary.queueKey,
        model: summary.model,
        agent: summary.agent,
      })),
    [agentSummaries, agentColorByKey]
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
              {METRIC_DEFS[metric].axisLabel}{" "}
            </span>
            <span style={{ fontWeight: 500 }}>
              {datum.estimatePrefix}
              {METRIC_DEFS[metric].formatValue(datum.x)}
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
  // right-anchored at the right edge, below the point at the top.
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

  const viewerEmail =
    user?.primaryEmailAddress?.emailAddress?.toLowerCase() ?? null;
  if (
    viewerEmail == null ||
    !PARETO_GRAPH_USER_ALLOWLIST.has(viewerEmail) ||
    metric == null ||
    points.length === 0
  ) {
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
                    ? `Plot pass@1 against average ${METRIC_DEFS[candidate].label} per trial`
                    : `No ${METRIC_DEFS[candidate].label} data reported`
                }
                className={`rounded-[5px] border px-2 py-0.5 font-mono text-[10.5px] leading-[1.6] transition-colors select-none ${
                  isActive
                    ? "border-[color:var(--paper-ink)] bg-[color:var(--paper-ink)] text-[color:var(--paper-bg)]"
                    : enabled
                      ? "cursor-pointer border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] text-[color:var(--paper-ink-2)] hover:border-[color:var(--paper-ink-4)] hover:bg-[color:var(--paper-surface-2)]"
                      : "cursor-default border-[color:var(--paper-line-2)] bg-transparent text-[color:var(--paper-ink-4)]"
                }`}
              >
                {METRIC_DEFS[candidate].label}
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
                tickFormatter={(v) => METRIC_DEFS[metric].formatTick(Number(v))}
                tick={{
                  fontSize: 10.5,
                  fill: "var(--paper-ink-2)",
                  fontFamily: "var(--font-geist-mono), ui-monospace, monospace",
                }}
                stroke="var(--paper-line)"
                label={{
                  value: METRIC_DEFS[metric].axisLabel,
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
              {/* The frontier itself: a dashed curve through the
                  non-dominated agents (a derived boundary, not a data
                  series — hence dashed). */}
              <Line
                data={frontierData}
                dataKey="y"
                type="linear"
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
              <Scatter
                data={visibleData}
                isAnimationActive={false}
                style={{ cursor: "pointer" }}
                onMouseEnter={(point: unknown) =>
                  onHoverAgent?.(
                    (point as { payload?: ChartDatum } | null)?.payload?.key ??
                      null
                  )
                }
                onMouseLeave={() => onHoverAgent?.(null)}
              >
                {visibleData.map((datum) => {
                  const isHovered = hoverAgent === datum.key;
                  const isDimmed =
                    hoverAgent != null && hoverAgent !== datum.key;
                  return (
                    <Cell
                      key={datum.key}
                      fill={agentColorByKey[datum.key] ?? AGENT_COLORS[0]}
                      fillOpacity={isDimmed ? 0.25 : 1}
                      stroke="var(--paper-surface)"
                      strokeWidth={isHovered ? 2.5 : 2}
                    />
                  );
                })}
              </Scatter>
            </ComposedChart>
          </ResponsiveContainer>
        ) : null}
      </div>

      <AgentLegend
        items={legendItems}
        hiddenKeys={hiddenAgents}
        onToggle={onToggleAgent}
        hoverKey={hoverAgent ?? null}
        onHover={onHoverAgent}
      />
    </div>
  );
});

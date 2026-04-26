"use client";

import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { TooltipContentProps } from "recharts";
import type { Task, Trial } from "@/lib/types";
import { calculatePassAtKCurve, type AgentPassAtKStats } from "@/lib/pass-at-k";
import { getExperimentAgentKey } from "@/lib/experiment-agent-grouping";
import type { AgentSummary } from "./experiment-trials-table";
import { AgentLegend } from "@/components/agent-legend";

// Color palette for different agents — kept stable between chart and
// leaderboard so the cross-highlight feels coherent.
export const AGENT_COLORS = [
  "#10b981", // emerald
  "#3b82f6", // blue
  "#f59e0b", // amber
  "#ef4444", // red
  "#8b5cf6", // violet
  "#ec4899", // pink
  "#06b6d4", // cyan
  "#84cc16", // lime
  "#f97316", // orange
  "#6366f1", // indigo
];

interface PassAtKGraphProps {
  tasks: Task[];
  agentSummaries: AgentSummary[];
  hiddenAgents: Set<string>;
  onToggleAgent: (agent: string) => void;
  hoverAgent?: string | null;
  onHoverAgent?: (key: string | null) => void;
}

function buildAgentStats(
  tasks: Task[],
  agentSummaries: AgentSummary[],
): { agentStats: Record<string, AgentPassAtKStats>; maxN: number } {
  const modelScopedAgents = new Set(
    agentSummaries
      .filter((summary) => summary.isModelScoped)
      .map((summary) => summary.agent),
  );

  let maxN = 1;
  const taskAgentTrials: Record<string, Record<string, Trial[]>> = {};

  for (const task of tasks) {
    if (!task.trials || task.trials.length === 0) continue;

    taskAgentTrials[task.id] = {};
    for (const trial of task.trials) {
      const key = getExperimentAgentKey(trial, modelScopedAgents);
      if (!taskAgentTrials[task.id][key]) {
        taskAgentTrials[task.id][key] = [];
      }
      taskAgentTrials[task.id][key].push(trial);
    }

    for (const agentTrials of Object.values(taskAgentTrials[task.id])) {
      maxN = Math.max(maxN, agentTrials.length);
    }
  }

  const agentStats: Record<string, AgentPassAtKStats> = {};
  for (const summary of agentSummaries) {
    const taskResults: { task: string; c: number }[] = [];
    for (const task of tasks) {
      const trials = taskAgentTrials[task.id]?.[summary.key] ?? [];
      if (trials.length === 0) continue;
      const c = trials.filter((t) => t.reward === 1).length;
      taskResults.push({ task: task.id, c });
    }
    agentStats[summary.key] = { n: maxN, taskResults };
  }

  return { agentStats, maxN };
}

export const PassAtKGraph = memo(function PassAtKGraph({
  tasks,
  agentSummaries,
  hiddenAgents,
  onToggleAgent,
  hoverAgent,
  onHoverAgent,
}: PassAtKGraphProps) {
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const [chartSize, setChartSize] = useState({ width: 0, height: 0 });
  const visibleAgentSummaries = useMemo(
    () => agentSummaries.filter((summary) => !hiddenAgents.has(summary.key)),
    [agentSummaries, hiddenAgents],
  );

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

  const { data, maxK, hasMultipleAttempts, agentColorByKey, agentLabelByKey } =
    useMemo(() => {
      const { agentStats, maxN } = buildAgentStats(tasks, agentSummaries);
      const curveData = maxN > 1 ? calculatePassAtKCurve(agentStats, maxN) : [];

      const colorMap: Record<string, string> = {};
      const labelMap: Record<string, string> = {};
      for (let i = 0; i < agentSummaries.length; i++) {
        colorMap[agentSummaries[i].key] = AGENT_COLORS[i % AGENT_COLORS.length];
        labelMap[agentSummaries[i].key] = agentSummaries[i].label;
      }

      return {
        data: curveData,
        maxK: maxN,
        hasMultipleAttempts: maxN > 1 && curveData.length > 0,
        agentColorByKey: colorMap,
        agentLabelByKey: labelMap,
      };
    }, [tasks, agentSummaries]);

  const renderTooltip = useCallback(
    (props: TooltipContentProps) => {
      const { active, payload, label } = props;
      if (!active || !payload || payload.length === 0) return null;

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
            maxHeight: "300px",
            overflowY: "auto",
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
            k = {label}
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
                      agentColorByKey[key] ??
                      (typeof entry.color === "string"
                        ? entry.color
                        : "var(--paper-ink-3)"),
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
                  {`${((Number(entry.value) || 0) * 100).toFixed(1)}%`}
                </span>
              </div>
            );
          })}
        </div>
      );
    },
    [agentColorByKey, agentLabelByKey, hoverAgent],
  );

  if (!hasMultipleAttempts) {
    return null;
  }

  return (
    <div className="flex h-full min-w-0 flex-col rounded-[10px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-4 py-3">
      <div className="mb-2 flex items-baseline justify-between gap-3">
        <h3 className="font-display text-[15px] font-medium tracking-[-0.01em] text-[color:var(--paper-ink)]">
          Pass@k
        </h3>
        <span className="font-mono text-[10.5px] text-[color:var(--paper-ink-3)]">
          n = {maxK} · {tasks.length} tasks · {agentSummaries.length} agents
        </span>
      </div>

      <div ref={chartContainerRef} className="h-52 min-w-0">
        {chartSize.width > 0 && chartSize.height > 0 ? (
          <ResponsiveContainer
            width={chartSize.width}
            height={chartSize.height}
          >
            <LineChart
              data={data}
              margin={{ top: 5, right: 16, left: 0, bottom: 5 }}
            >
              <CartesianGrid
                strokeDasharray="2 4"
                stroke="var(--paper-line-2)"
                vertical={false}
              />
              <XAxis
                dataKey="k"
                tick={{
                  fontSize: 10.5,
                  fill: "var(--paper-ink-2)",
                  fontFamily: "var(--font-geist-mono), ui-monospace, monospace",
                }}
                stroke="var(--paper-line)"
                label={{
                  value: "k",
                  position: "insideBottomRight",
                  offset: -5,
                  fontSize: 10,
                  fontStyle: "italic",
                  fill: "var(--paper-ink-3)",
                }}
              />
              <YAxis
                domain={[0, 1]}
                tickFormatter={(v) => `${Math.round(v * 100)}%`}
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
              {visibleAgentSummaries.map((summary) => {
                const color = agentColorByKey[summary.key] ?? AGENT_COLORS[0];
                const isHovered = hoverAgent === summary.key;
                const isDimmed =
                  hoverAgent != null && hoverAgent !== summary.key;
                return (
                  <Line
                    key={summary.key}
                    type="monotone"
                    dataKey={summary.key}
                    stroke={color}
                    strokeWidth={isHovered ? 2.6 : 2}
                    strokeOpacity={isDimmed ? 0.2 : 1}
                    dot={{
                      r: isHovered ? 4 : 3,
                      fill: "var(--paper-surface)",
                      stroke: color,
                      strokeWidth: 2,
                      strokeOpacity: isDimmed ? 0.25 : 1,
                    }}
                    activeDot={{
                      r: 5,
                      fill: "var(--paper-surface)",
                      stroke: color,
                      strokeWidth: 2,
                    }}
                    isAnimationActive={false}
                    onMouseEnter={() => onHoverAgent?.(summary.key)}
                    onMouseLeave={() => onHoverAgent?.(null)}
                    style={{ cursor: "pointer" }}
                  />
                );
              })}
            </LineChart>
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

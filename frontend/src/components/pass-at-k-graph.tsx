"use client";

import { memo, useMemo, useCallback } from "react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import type { TooltipProps } from "recharts";
import type { Task, Trial } from "@/lib/types";
import { calculatePassAtKCurve, type AgentPassAtKStats } from "@/lib/pass-at-k";
import { getExperimentAgentKey } from "@/lib/experiment-agent-grouping";
import type { AgentSummary } from "./experiment-trials-table";
import { Card, CardContent } from "@/components/ui/card";
import { AgentLegend } from "@/components/agent-legend";

// Color palette for different agents
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
}

/**
 * Transform tasks with trials into agent-centric stats for pass@k calculation
 */
function buildAgentStats(
  tasks: Task[],
  agentSummaries: AgentSummary[],
): { agentStats: Record<string, AgentPassAtKStats>; maxN: number } {
  const modelScopedAgents = new Set(
    agentSummaries
      .filter((summary) => summary.isModelScoped)
      .map((summary) => summary.agent),
  );

  // First, determine the max number of trials per task-agent combination
  let maxN = 1;

  // Group trials by task and agent
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

    // Update maxN based on this task's trials per agent
    for (const agentTrials of Object.values(taskAgentTrials[task.id])) {
      maxN = Math.max(maxN, agentTrials.length);
    }
  }

  // Build agent stats
  const agentStats: Record<string, AgentPassAtKStats> = {};

  for (const summary of agentSummaries) {
    const taskResults: { task: string; c: number }[] = [];

    for (const task of tasks) {
      const trials = taskAgentTrials[task.id]?.[summary.key] ?? [];
      if (trials.length === 0) continue;

      // Count passing trials (reward === 1)
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
}: PassAtKGraphProps) {
  const visibleAgentSummaries = useMemo(
    () => agentSummaries.filter((summary) => !hiddenAgents.has(summary.key)),
    [agentSummaries, hiddenAgents],
  );

  const { data, maxK, hasMultipleAttempts } = useMemo(() => {
    const { agentStats, maxN } = buildAgentStats(tasks, agentSummaries);

    // Check if we have any multi-attempt data
    if (maxN <= 1) {
      return { data: [], maxK: 0, hasMultipleAttempts: false };
    }

    const curveData = calculatePassAtKCurve(agentStats, maxN);

    return { data: curveData, maxK: maxN, hasMultipleAttempts: true };
  }, [tasks, agentSummaries]);

  // Build agent color map for tooltip
  const agentColorMap = useMemo(() => {
    const map: Record<string, string> = {};
    for (let i = 0; i < agentSummaries.length; i++) {
      map[agentSummaries[i].key] = AGENT_COLORS[i % AGENT_COLORS.length];
    }
    return map;
  }, [agentSummaries]);

  // Build agent label map for tooltip
  const agentLabelMap = useMemo(() => {
    const map: Record<string, string> = {};
    for (const summary of agentSummaries) {
      map[summary.key] = summary.label;
    }
    return map;
  }, [agentSummaries]);

  // Custom tooltip that sorts entries by value (descending) to match visual line order
  const renderTooltip = useCallback(
    (props: TooltipProps<number, string>) => {
      const { active, payload, label } = props;
      if (!active || !payload || payload.length === 0) return null;

      const sorted = [...payload]
        .filter((entry) => entry.value !== undefined)
        .sort((a, b) => (b.value ?? 0) - (a.value ?? 0));

      return (
        <div
          style={{
            backgroundColor: "hsl(var(--card))",
            border: "1px solid hsl(var(--border))",
            borderRadius: "6px",
            padding: "8px 12px",
            fontSize: "12px",
            fontFamily: "monospace",
            boxShadow: "0 2px 8px rgba(0,0,0,0.15)",
            maxHeight: "300px",
            overflowY: "auto",
          }}
        >
          <div style={{ marginBottom: "4px", fontWeight: 600 }}>k = {label}</div>
          {sorted.map((entry) => (
            <div
              key={entry.dataKey}
              style={{
                display: "flex",
                alignItems: "center",
                gap: "6px",
                padding: "1px 0",
                whiteSpace: "nowrap",
              }}
            >
              <span
                style={{
                  width: "8px",
                  height: "8px",
                  borderRadius: "2px",
                  backgroundColor: agentColorMap[entry.dataKey as string] ?? entry.color,
                  flexShrink: 0,
                }}
              />
              <span style={{ color: "hsl(var(--muted-foreground))" }}>
                {agentLabelMap[entry.dataKey as string] ?? entry.dataKey}
              </span>
              <span style={{ marginLeft: "auto", paddingLeft: "12px", fontWeight: 500 }}>
                {`${((entry.value ?? 0) * 100).toFixed(1)}%`}
              </span>
            </div>
          ))}
        </div>
      );
    },
    [agentColorMap, agentLabelMap],
  );

  // Don't render if no multi-attempt data
  if (!hasMultipleAttempts || data.length === 0) {
    return null;
  }

  return (
    <Card className="h-full bg-card/80 shadow-sm">
      <CardContent className="flex h-full flex-col p-6">
        <h3 className="font-mono text-sm font-bold text-foreground">
          Pass@k{" "}
          <span className="font-normal text-muted-foreground">
            (n = {maxK})
          </span>
        </h3>

        <div className="mt-4 h-52">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart
              data={data}
              margin={{ top: 5, right: 30, left: 0, bottom: 5 }}
            >
              <CartesianGrid strokeDasharray="3 3" className="stroke-border" />
              <XAxis
                dataKey="k"
                tick={{ fontSize: 11 }}
                className="font-mono"
                label={{
                  value: "k",
                  position: "insideBottomRight",
                  offset: -5,
                  fontSize: 11,
                }}
              />
              <YAxis
                domain={[0, 1]}
                tickFormatter={(v) => `${Math.round(v * 100)}%`}
                tick={{ fontSize: 11 }}
                className="font-mono"
              />
              <Tooltip
                content={renderTooltip}
                wrapperStyle={{ zIndex: 10 }}
              />
              {visibleAgentSummaries.map((summary) => {
                const originalIdx = agentSummaries.findIndex(
                  (agent) => agent.key === summary.key,
                );
                return (
                  <Line
                    key={summary.key}
                    type="monotone"
                    dataKey={summary.key}
                    stroke={AGENT_COLORS[originalIdx % AGENT_COLORS.length]}
                    strokeWidth={2}
                    dot={{ r: 3 }}
                    activeDot={{ r: 5 }}
                  />
                );
              })}
            </LineChart>
          </ResponsiveContainer>
        </div>

        <AgentLegend
          items={agentSummaries.map((summary, idx) => ({
            key: summary.key,
            label: summary.label,
            color: AGENT_COLORS[idx % AGENT_COLORS.length],
          }))}
          hiddenKeys={hiddenAgents}
          onToggle={onToggleAgent}
        />
      </CardContent>
    </Card>
  );
});

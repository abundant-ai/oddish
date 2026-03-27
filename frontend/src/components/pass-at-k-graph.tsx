"use client";

import { memo, useMemo } from "react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
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
                formatter={(
                  value: number | undefined,
                  name: string | undefined,
                ) => [
                  value !== undefined ? `${(value * 100).toFixed(1)}%` : "N/A",
                  name ?? "",
                ]}
                labelFormatter={(k) => `k = ${k}`}
                contentStyle={{
                  backgroundColor: "hsl(var(--card))",
                  border: "1px solid hsl(var(--border))",
                  borderRadius: "6px",
                  fontSize: "12px",
                  fontFamily: "monospace",
                }}
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

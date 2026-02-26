"use client";

import { memo, useMemo } from "react";
import type { Task, Trial } from "@/lib/types";
import { Button } from "@/components/ui/button";
import type { AgentSummary } from "./experiment-trials-table";
import { QueueKeyIcon } from "./queue-key-icon";
import { AGENT_COLORS } from "./pass-at-k-graph";

interface PassAtOneLeaderboardProps {
  tasks: Task[];
  agentSummaries: AgentSummary[];
  hiddenAgents: Set<string>;
  onToggleAgent: (agent: string) => void;
}

type LeaderboardRow = {
  agent: string;
  model: string | null;
  queueKey: string | null;
  mean: number;
};

const GRADIENTS: Array<[string, string]> = [
  ["#f59e0b", "#facc15"],
  ["#3b82f6", "#ec4899"],
  ["#93c5fd", "#fef08a"],
  ["#fde047", "#f59e0b"],
  ["#2563eb", "#a855f7"],
  ["#22d3ee", "#a3e635"],
];

function getPassAtOneValue(trials: Trial[]): number | null {
  if (trials.length === 0) return null;
  const passing = trials.filter((trial) => trial.reward === 1).length;
  return passing / trials.length;
}

function calculateRows(
  tasks: Task[],
  agentSummaries: AgentSummary[],
): LeaderboardRow[] {
  const rows: LeaderboardRow[] = [];

  for (const summary of agentSummaries) {
    const taskValues: number[] = [];
    for (const task of tasks) {
      const trials = (task.trials ?? []).filter(
        (trial) => trial.agent === summary.agent,
      );
      const value = getPassAtOneValue(trials);
      if (value !== null) {
        taskValues.push(value);
      }
    }

    if (taskValues.length === 0) continue;

    const mean =
      taskValues.reduce((acc, value) => acc + value, 0) / taskValues.length;
    rows.push({
      agent: summary.agent,
      model: summary.model,
      queueKey: summary.queueKey,
      mean,
    });
  }

  return rows.sort((a, b) => b.mean - a.mean);
}

export const PassAtOneLeaderboard = memo(function PassAtOneLeaderboard({
  tasks,
  agentSummaries,
  hiddenAgents,
  onToggleAgent,
}: PassAtOneLeaderboardProps) {
  const rows = useMemo(
    () => calculateRows(tasks, agentSummaries),
    [tasks, agentSummaries],
  );
  const visibleRows = useMemo(
    () => rows.filter((row) => !hiddenAgents.has(row.agent)),
    [rows, hiddenAgents],
  );
  const colorByAgent = useMemo(() => {
    const colors = new Map<string, string>();
    for (const [idx, summary] of agentSummaries.entries()) {
      colors.set(summary.agent, AGENT_COLORS[idx % AGENT_COLORS.length]);
    }
    return colors;
  }, [agentSummaries]);

  if (rows.length === 0) {
    return null;
  }

  const domain = 1;
  const ticks = [0, 0.2, 0.4, 0.6, 0.8, 1];

  return (
    <div className="rounded-lg border border-border bg-card/80 p-6 shadow-sm h-full">
      <div className="flex h-full flex-col">
        <div className="flex items-center justify-between gap-4">
          <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Leaderboard
          </div>
          <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Score
          </div>
        </div>

        <div className="mt-4 space-y-3">
          {visibleRows.map((row, index) => {
            const [start, end] = GRADIENTS[index % GRADIENTS.length];
            const width = (row.mean / domain) * 100;

            return (
              <div key={row.agent} className="min-w-0 space-y-1.5">
                <div className="flex items-center justify-between gap-4 text-sm text-foreground">
                  <div className="flex min-w-0 items-center justify-start gap-1.5">
                    <QueueKeyIcon
                      queueKey={row.queueKey}
                      model={row.model}
                      agent={row.agent}
                      size={14}
                      className="shrink-0 text-muted-foreground"
                    />
                    <span className="truncate">
                      <span className="font-semibold">{row.agent}</span>
                    </span>
                  </div>
                  <div className="text-right font-mono text-sm text-foreground">
                    {(row.mean * 100).toFixed(1)}%
                  </div>
                </div>
                <div className="relative h-3 rounded-full bg-muted/70">
                  <div
                    className="h-full rounded-full"
                    style={{
                      width: `${width}%`,
                      background: `linear-gradient(90deg, ${start} 0%, ${end} 100%)`,
                    }}
                  />
                </div>
              </div>
            );
          })}
          {visibleRows.length === 0 && (
            <div className="py-6 text-center text-xs text-muted-foreground">
              All agents are hidden.
            </div>
          )}
        </div>

        <div className="relative mt-auto pt-4">
          <div className="absolute inset-x-0 top-0 border-t border-border" />
          <div className="flex justify-between pt-4 font-mono text-xs text-muted-foreground">
            {ticks.map((tick) => (
              <span key={tick}>{Math.round(tick * 100)}%</span>
            ))}
          </div>
        </div>

        <div className="mt-3 flex flex-wrap gap-2">
          {rows.map((row) => {
            const isHidden = hiddenAgents.has(row.agent);
            const color = colorByAgent.get(row.agent) ?? AGENT_COLORS[0];
            return (
              <Button
                key={row.agent}
                type="button"
                onClick={() => onToggleAgent(row.agent)}
                variant="ghost"
                size="sm"
                className={`h-auto flex items-center gap-2 rounded px-2 py-1 font-mono text-xs transition-all ${
                  isHidden ? "opacity-40 hover:opacity-60" : "hover:bg-muted"
                }`}
                title={isHidden ? "Click to show" : "Click to hide"}
              >
                <span
                  className="h-3 w-3 flex-shrink-0 rounded-sm"
                  style={{
                    backgroundColor: isHidden ? "transparent" : color,
                    border: `2px solid ${color}`,
                  }}
                />
                <span
                  className={
                    isHidden ? "text-muted-foreground line-through" : ""
                  }
                >
                  {row.agent}
                </span>
              </Button>
            );
          })}
        </div>
      </div>
    </div>
  );
});

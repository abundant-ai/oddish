"use client";

import { memo, useMemo } from "react";
import type { Task, Trial } from "@/lib/types";
import { getExperimentAgentKey } from "@/lib/experiment-agent-grouping";
import type { AgentSummary } from "./experiment-trials-table";
import { AGENT_COLORS } from "./pass-at-k-graph";

interface PassAtOneLeaderboardProps {
  tasks: Task[];
  agentSummaries: AgentSummary[];
  hiddenAgents: Set<string>;
  onToggleAgent?: (agent: string) => void;
  hoverAgent?: string | null;
  onHoverAgent?: (key: string | null) => void;
}

type LeaderboardRow = {
  key: string;
  label: string;
  agent: string;
  model: string | null;
  queueKey: string | null;
  mean: number;
};

function getPassAtOneValue(trials: Trial[]): number | null {
  if (trials.length === 0) return null;
  const passing = trials.filter((trial) => trial.reward === 1).length;
  return passing / trials.length;
}

function calculateRows(
  tasks: Task[],
  agentSummaries: AgentSummary[],
): LeaderboardRow[] {
  const modelScopedAgents = new Set(
    agentSummaries
      .filter((summary) => summary.isModelScoped)
      .map((summary) => summary.agent),
  );
  const rows: LeaderboardRow[] = [];

  for (const summary of agentSummaries) {
    const taskValues: number[] = [];
    for (const task of tasks) {
      const trials = (task.trials ?? []).filter(
        (trial) =>
          getExperimentAgentKey(trial, modelScopedAgents) === summary.key,
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
      key: summary.key,
      label: summary.label,
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
  hoverAgent,
  onHoverAgent,
}: PassAtOneLeaderboardProps) {
  const rows = useMemo(
    () => calculateRows(tasks, agentSummaries),
    [tasks, agentSummaries],
  );
  const visibleRows = useMemo(
    () => rows.filter((row) => !hiddenAgents.has(row.key)),
    [rows, hiddenAgents],
  );
  const colorByAgent = useMemo(() => {
    const colors = new Map<string, string>();
    for (const [idx, summary] of agentSummaries.entries()) {
      colors.set(summary.key, AGENT_COLORS[idx % AGENT_COLORS.length]);
    }
    return colors;
  }, [agentSummaries]);

  if (rows.length === 0) {
    return null;
  }

  const domain = 1;
  const scaleTicks = [0, 0.25, 0.5, 0.75, 1];

  return (
    <div className="flex h-full min-w-0 flex-col rounded-[10px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-4 py-3">
      <div className="mb-2 flex items-baseline justify-between gap-3">
        <h3 className="font-display text-[15px] font-medium tracking-[-0.01em] text-[color:var(--paper-ink)]">
          Leaderboard
        </h3>
        <span className="font-mono text-[10.5px] text-[color:var(--paper-ink-3)]">
          score = (pass + ½·partial) / completed
        </span>
      </div>

      <div className="grid grid-cols-[1fr_60px] border-b border-[color:var(--paper-line-2)] pb-1.5 font-mono text-[9.5px] font-semibold uppercase tracking-[0.12em] text-[color:var(--paper-ink-3)]">
        <span>Agent</span>
        <span className="text-right">Score</span>
      </div>

      <div className="flex flex-col">
        {visibleRows.map((row, index) => {
          const color = colorByAgent.get(row.key) ?? AGENT_COLORS[0];
          const width = (row.mean / domain) * 100;
          const isDim = hoverAgent != null && hoverAgent !== row.key;
          const mark = (row.label || "A").charAt(0).toUpperCase();
          const isLast = index === visibleRows.length - 1;

          return (
            <div
              key={row.key}
              className={`grid grid-cols-[1fr_60px] items-center pb-1.5 pt-2 transition-opacity ${
                isLast
                  ? ""
                  : "border-b border-dashed border-[color:var(--paper-line-2)]"
              }`}
              style={{ opacity: isDim ? 0.32 : 1 }}
              onMouseEnter={() => onHoverAgent?.(row.key)}
              onMouseLeave={() => onHoverAgent?.(null)}
            >
              <div className="flex min-w-0 items-center gap-2">
                <span
                  className="flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-[3px] font-mono text-[10px] font-bold text-white"
                  style={{ backgroundColor: color }}
                  aria-hidden="true"
                >
                  {mark}
                </span>
                <span className="truncate font-mono text-[11.5px] text-[color:var(--paper-ink)]">
                  {row.label}
                </span>
              </div>
              <div className="text-right font-mono text-xs font-semibold tracking-[-0.01em] text-[color:var(--paper-ink)]">
                {(row.mean * 100).toFixed(1)}%
              </div>
              <div className="col-span-2 mt-1.5 h-1.5 overflow-hidden rounded-full bg-[color:var(--paper-bg-2)]">
                <div
                  className="h-full rounded-full transition-[width] duration-200"
                  style={{
                    width: `${width}%`,
                    background: `linear-gradient(90deg, color-mix(in oklch, ${color}, white 18%) 0%, ${color} 100%)`,
                  }}
                />
              </div>
            </div>
          );
        })}
        {visibleRows.length === 0 && (
          <div className="py-6 text-center text-xs text-[color:var(--paper-ink-3)]">
            All agents are hidden.
          </div>
        )}
      </div>

      <div className="mt-auto flex justify-between border-t border-[color:var(--paper-line-2)] pt-2 font-mono text-[9.5px] text-[color:var(--paper-ink-3)]">
        {scaleTicks.map((tick) => (
          <span key={tick}>{Math.round(tick * 100)}%</span>
        ))}
      </div>
    </div>
  );
});

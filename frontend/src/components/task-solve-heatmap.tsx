"use client";

import { memo, useMemo } from "react";
import type { Task } from "@/lib/types";
import {
  getExperimentAgentKey,
  getModelScopedAgentsFromSummaries,
  type ExperimentAgentSummary,
} from "@/lib/experiment-agent-grouping";
import { QueueKeyIcon } from "./queue-key-icon";

interface TaskSolveHeatmapProps {
  tasks: Task[];
  agentSummaries: ExperimentAgentSummary[];
  hiddenAgents: Set<string>;
}

type Cell = { n: number; c: number } | null;

type HeatmapRow = {
  taskId: string;
  taskName: string;
  cells: Cell[];
  /** Mean solve rate over the agents that ran the task (the sort key). */
  mean: number;
};

// Cell fill uses the dashboard's semantic status tokens: a pass ramp whose
// strength is the solve rate, a fixed fail tint for ran-and-never-passed,
// and near-nothing for never-ran.
function cellBackground(cell: Cell): string {
  if (cell == null)
    return "color-mix(in oklch, var(--paper-ink-4) 12%, transparent)";
  if (cell.c === 0)
    return "color-mix(in oklch, var(--paper-fail) 28%, transparent)";
  const strength = Math.round(20 + 80 * (cell.c / cell.n));
  return `color-mix(in oklch, var(--paper-pass) ${strength}%, transparent)`;
}

export const TaskSolveHeatmap = memo(function TaskSolveHeatmap({
  tasks,
  agentSummaries,
  hiddenAgents,
}: TaskSolveHeatmapProps) {
  const visibleSummaries = useMemo(
    () => agentSummaries.filter((summary) => !hiddenAgents.has(summary.key)),
    [agentSummaries, hiddenAgents]
  );

  const rows = useMemo(() => {
    const modelScopedAgents = getModelScopedAgentsFromSummaries(agentSummaries);
    const built: HeatmapRow[] = [];
    for (const task of tasks) {
      const byAgent = new Map<string, { n: number; c: number }>();
      for (const trial of task.trials ?? []) {
        const key = getExperimentAgentKey(trial, modelScopedAgents);
        const bucket = byAgent.get(key) ?? { n: 0, c: 0 };
        bucket.n += 1;
        if (trial.reward === 1) bucket.c += 1;
        byAgent.set(key, bucket);
      }
      const cells = visibleSummaries.map(
        (summary) => byAgent.get(summary.key) ?? null
      );
      const ran = cells.filter((cell): cell is { n: number; c: number } =>
        Boolean(cell)
      );
      if (ran.length === 0) continue;
      built.push({
        taskId: task.id,
        taskName: task.name,
        cells,
        mean: ran.reduce((acc, cell) => acc + cell.c / cell.n, 0) / ran.length,
      });
    }
    // Hardest first: the unsolved rows are the ones worth reading.
    return built.sort((a, b) => a.mean - b.mean);
  }, [tasks, agentSummaries, visibleSummaries]);

  if (rows.length === 0 || visibleSummaries.length === 0) {
    return null;
  }

  const gridTemplateColumns = `minmax(120px,220px) repeat(${visibleSummaries.length}, minmax(22px,1fr))`;

  return (
    <div className="flex h-full min-w-0 flex-col rounded-[10px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-4 py-3">
      <div className="mb-2 flex items-baseline justify-between gap-3">
        <h3 className="font-display text-[15px] font-medium tracking-[-0.01em] text-[color:var(--paper-ink)]">
          Solve grid
        </h3>
        <span className="font-mono text-[10.5px] text-[color:var(--paper-ink-3)]">
          per-task pass rate · hardest first
        </span>
      </div>

      <div className="max-h-64 min-w-0 overflow-auto">
        <div
          className="sticky top-0 z-10 grid items-center gap-px bg-[color:var(--paper-surface)] pb-1"
          style={{ gridTemplateColumns }}
        >
          <span />
          {visibleSummaries.map((summary) => (
            <span
              key={summary.key}
              title={summary.label}
              className="flex justify-center"
            >
              <QueueKeyIcon
                queueKey={summary.queueKey}
                model={summary.model}
                agent={summary.agent}
                size={13}
              />
            </span>
          ))}
        </div>
        {rows.map((row) => (
          <div
            key={row.taskId}
            className="grid items-center gap-px"
            style={{ gridTemplateColumns }}
          >
            <span
              title={row.taskName}
              className="truncate pr-2 font-mono text-[10.5px] leading-[18px] text-[color:var(--paper-ink-2)]"
            >
              {row.taskName}
            </span>
            {row.cells.map((cell, i) => (
              <span
                key={visibleSummaries[i].key}
                title={
                  cell
                    ? `${row.taskName} · ${visibleSummaries[i].label}: ${cell.c}/${cell.n} passed`
                    : `${row.taskName} · ${visibleSummaries[i].label}: not run`
                }
                className="h-[14px] rounded-[3px]"
                style={{ background: cellBackground(cell) }}
              />
            ))}
          </div>
        ))}
      </div>

      <div className="mt-2 flex items-center gap-3 font-mono text-[9.5px] text-[color:var(--paper-ink-3)]">
        <span className="inline-flex items-center gap-1.5">
          <i
            className="inline-block h-2 w-4 rounded-[2px]"
            style={{ background: cellBackground({ n: 1, c: 1 }) }}
          />
          all pass
        </span>
        <span className="inline-flex items-center gap-1.5">
          <i
            className="inline-block h-2 w-4 rounded-[2px]"
            style={{ background: cellBackground({ n: 2, c: 1 }) }}
          />
          some
        </span>
        <span className="inline-flex items-center gap-1.5">
          <i
            className="inline-block h-2 w-4 rounded-[2px]"
            style={{ background: cellBackground({ n: 1, c: 0 }) }}
          />
          none
        </span>
        <span className="inline-flex items-center gap-1.5">
          <i
            className="inline-block h-2 w-4 rounded-[2px]"
            style={{ background: cellBackground(null) }}
          />
          not run
        </span>
      </div>
    </div>
  );
});

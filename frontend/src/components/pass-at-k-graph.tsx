"use client";

import { memo, useMemo } from "react";
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
  const { series, maxK, hasMultipleAttempts, agentColorByKey, agentLabelByKey } =
    useMemo(() => {
      const { agentStats, maxN } = buildAgentStats(tasks, agentSummaries);
      const curveData =
        maxN > 1 ? calculatePassAtKCurve(agentStats, maxN) : [];

      const colorMap: Record<string, string> = {};
      const labelMap: Record<string, string> = {};
      for (let i = 0; i < agentSummaries.length; i++) {
        colorMap[agentSummaries[i].key] = AGENT_COLORS[i % AGENT_COLORS.length];
        labelMap[agentSummaries[i].key] = agentSummaries[i].label;
      }

      const s = agentSummaries
        .filter((summary) => !hiddenAgents.has(summary.key))
        .map((summary) => ({
          key: summary.key,
          label: summary.label,
          color: colorMap[summary.key],
          points: curveData.map((row) => {
            const raw = (row as Record<string, unknown>)[summary.key];
            return typeof raw === "number" && Number.isFinite(raw)
              ? Math.max(0, Math.min(1, raw))
              : 0;
          }),
        }));

      return {
        series: s,
        maxK: maxN,
        hasMultipleAttempts: maxN > 1 && curveData.length > 0,
        agentColorByKey: colorMap,
        agentLabelByKey: labelMap,
      };
    }, [tasks, agentSummaries, hiddenAgents]);

  if (!hasMultipleAttempts) {
    return null;
  }

  // SVG chart geometry (matches design reference viewBox 400x180)
  const W = 400;
  const H = 180;
  const padL = 34;
  const padR = 12;
  const padT = 10;
  const padB = 28;
  const cw = W - padL - padR;
  const ch = H - padT - padB;
  const xAt = (k: number) => padL + ((k - 1) / Math.max(1, maxK - 1)) * cw;
  const yAt = (v: number) => padT + (1 - v) * ch;
  const gridVals = [0, 0.25, 0.5, 0.75, 1.0];

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

      <div className="relative w-full" style={{ aspectRatio: "16 / 5.5" }}>
        <svg
          viewBox={`0 0 ${W} ${H}`}
          preserveAspectRatio="none"
          className="block h-full w-full overflow-visible"
          role="img"
          aria-label="Pass@k line chart"
        >
          {/* Grid lines + y labels */}
          {gridVals.map((v) => (
            <g key={v}>
              <line
                x1={padL}
                x2={W - padR}
                y1={yAt(v)}
                y2={yAt(v)}
                stroke="var(--paper-line-2)"
                strokeWidth={1}
                strokeDasharray="2 4"
              />
              <text
                x={padL - 6}
                y={yAt(v) + 3.5}
                textAnchor="end"
                style={{
                  fontFamily:
                    "var(--font-geist-mono), ui-monospace, monospace",
                  fontSize: 9.5,
                  fill: "var(--paper-ink-3)",
                }}
              >
                {Math.round(v * 100)}%
              </text>
            </g>
          ))}
          {/* X axis */}
          <line
            x1={padL}
            x2={W - padR}
            y1={padT + ch}
            y2={padT + ch}
            stroke="var(--paper-line)"
            strokeWidth={1}
          />
          {Array.from({ length: maxK }, (_, i) => i + 1).map((k) => (
            <text
              key={k}
              x={xAt(k)}
              y={padT + ch + 15}
              textAnchor="middle"
              style={{
                fontFamily:
                  "var(--font-geist-mono), ui-monospace, monospace",
                fontSize: 10.5,
                fill: "var(--paper-ink-2)",
              }}
            >
              {k}
            </text>
          ))}
          <text
            x={W - padR}
            y={padT + ch + 24}
            textAnchor="end"
            style={{
              fontFamily: "var(--font-geist-mono), ui-monospace, monospace",
              fontSize: 10,
              fontStyle: "italic",
              fill: "var(--paper-ink-3)",
            }}
          >
            k
          </text>

          {/* Series paths */}
          {series.map((s) => {
            const dim = hoverAgent != null && hoverAgent !== s.key;
            const hi = hoverAgent === s.key;
            const d = s.points
              .map(
                (v, i) =>
                  `${i === 0 ? "M" : "L"} ${xAt(i + 1).toFixed(2)} ${yAt(
                    v,
                  ).toFixed(2)}`,
              )
              .join(" ");
            return (
              <path
                key={s.key}
                d={d}
                fill="none"
                stroke={s.color}
                strokeWidth={hi ? 2.6 : 2}
                strokeLinecap="round"
                strokeLinejoin="round"
                opacity={dim ? 0.18 : 1}
                onMouseEnter={() => onHoverAgent?.(s.key)}
                onMouseLeave={() => onHoverAgent?.(null)}
                style={{ cursor: "pointer", transition: "stroke-width .15s, opacity .15s" }}
              />
            );
          })}
          {/* Series dots (on top of paths so they remain interactive) */}
          {series.map((s) => {
            const dim = hoverAgent != null && hoverAgent !== s.key;
            const hi = hoverAgent === s.key;
            return (
              <g
                key={`${s.key}-dots`}
                onMouseEnter={() => onHoverAgent?.(s.key)}
                onMouseLeave={() => onHoverAgent?.(null)}
              >
                {s.points.map((v, i) => (
                  <circle
                    key={i}
                    cx={xAt(i + 1)}
                    cy={yAt(v)}
                    r={hi ? 4.5 : 3.2}
                    fill="var(--paper-surface)"
                    stroke={s.color}
                    strokeWidth={2}
                    opacity={dim ? 0.22 : 1}
                  >
                    <title>
                      {agentLabelByKey[s.key] ?? s.key} · k={i + 1} ·{" "}
                      {(v * 100).toFixed(0)}%
                    </title>
                  </circle>
                ))}
              </g>
            );
          })}
        </svg>
      </div>

      <AgentLegend
        items={agentSummaries.map((summary, idx) => ({
          key: summary.key,
          label: summary.label,
          color:
            agentColorByKey[summary.key] ??
            AGENT_COLORS[idx % AGENT_COLORS.length],
        }))}
        hiddenKeys={hiddenAgents}
        onToggle={onToggleAgent}
        hoverKey={hoverAgent ?? null}
        onHover={onHoverAgent}
      />
    </div>
  );
});

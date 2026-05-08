"use client";

import { useCallback, useMemo } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  calculatePassAtKCurve,
  type AgentPassAtKStats,
} from "@/lib/pass-at-k";
import type {
  ExperimentCellAgent,
  ResolvedExperiment,
  ResolvedExperimentCell,
} from "@/lib/types";

const AGENT_COLORS = [
  "#10b981",
  "#3b82f6",
  "#f59e0b",
  "#ef4444",
  "#8b5cf6",
  "#ec4899",
  "#06b6d4",
  "#84cc16",
  "#f97316",
  "#6366f1",
];

export function colorForAgent(eqKey: string, agents: ExperimentCellAgent[]) {
  const idx = agents.findIndex((a) => a.equivalence_key === eqKey);
  return AGENT_COLORS[(idx >= 0 ? idx : 0) % AGENT_COLORS.length];
}

interface Props {
  data: ResolvedExperiment;
  hiddenAgents: Set<string>;
  onToggleAgent: (eqKey: string) => void;
}

function formatAgent(a: ExperimentCellAgent): string {
  const model = a.model && a.model !== "default" ? ` · ${a.model}` : "";
  return `${a.harness}${model}`;
}

export function ExperimentPassAtKGraph({
  data,
  hiddenAgents,
  onToggleAgent,
}: Props) {
  const { agents, cells } = data;

  const { points, maxK } = useMemo(() => {
    const cellsByAgent = new Map<string, ResolvedExperimentCell[]>();
    let globalN = 1;
    for (const c of cells) {
      const list = cellsByAgent.get(c.agent.equivalence_key) ?? [];
      list.push(c);
      cellsByAgent.set(c.agent.equivalence_key, list);
      if (c.have_n_total > globalN) globalN = c.have_n_total;
    }

    if (globalN < 2) return { points: [], maxK: 0 };

    const stats: Record<string, AgentPassAtKStats> = {};
    for (const a of agents) {
      const list = cellsByAgent.get(a.equivalence_key) ?? [];
      stats[a.equivalence_key] = {
        n: globalN,
        taskResults: list
          .filter((c) => c.have_n_total > 0)
          .map((c) => ({
            task: c.task_version_id,
            c: c.have_n_successful,
          })),
      };
    }
    return {
      points: calculatePassAtKCurve(stats, globalN),
      maxK: globalN,
    };
  }, [agents, cells]);

  const colorByAgent = useMemo(() => {
    const m: Record<string, string> = {};
    agents.forEach((a, i) => {
      m[a.equivalence_key] = AGENT_COLORS[i % AGENT_COLORS.length];
    });
    return m;
  }, [agents]);

  const labelByAgent = useMemo(() => {
    const m: Record<string, string> = {};
    for (const a of agents) m[a.equivalence_key] = formatAgent(a);
    return m;
  }, [agents]);

  const renderTooltip = useCallback(
    // recharts v3 doesn't expose a clean generic for the tooltip
    // payload, so we accept the loose shape and narrow inline.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (p: any) => {
      const { active, payload, label } = p as {
        active?: boolean;
        payload?: { dataKey?: string | number; value?: number; color?: string }[];
        label?: number;
      };
      if (!active || !payload || payload.length === 0) return null;
      const sorted = [...payload]
        .filter((e) => typeof e.value === "number")
        .sort((a, b) => (Number(b.value) || 0) - (Number(a.value) || 0));
      return (
        <div className="rounded-sm border bg-card p-2 font-mono text-xs shadow-md">
          <div className="mb-1 font-semibold">k = {label}</div>
          {sorted.map((entry) => {
            const key = entry.dataKey as string;
            return (
              <div
                key={key}
                className="flex items-center gap-1.5 whitespace-nowrap"
              >
                <span
                  className="h-2 w-2 rounded-sm"
                  style={{ backgroundColor: colorByAgent[key] }}
                />
                <span className="text-muted-foreground">
                  {labelByAgent[key] ?? key}
                </span>
                <span className="ml-auto pl-3 font-medium">
                  {`${((Number(entry.value) || 0) * 100).toFixed(1)}%`}
                </span>
              </div>
            );
          })}
        </div>
      );
    },
    [colorByAgent, labelByAgent],
  );

  if (points.length === 0) return null;

  const visible = agents.filter((a) => !hiddenAgents.has(a.equivalence_key));

  return (
    <div className="rounded-sm border bg-card p-4">
      <div className="space-y-3">
        <div>
          <h3 className="font-mono text-sm font-bold">
            Pass@k{" "}
            <span className="font-normal text-muted-foreground">
              (n = {maxK})
            </span>
          </h3>
          <p className="mt-1 text-xs text-muted-foreground">
            Probability that at least 1 of k sampled attempts passes,
            averaged across tasks.
          </p>
        </div>
        <div className="h-56">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart
              data={points}
              margin={{ top: 5, right: 12, left: 0, bottom: 5 }}
            >
              <CartesianGrid strokeDasharray="3 3" className="stroke-border" />
              <XAxis
                dataKey="k"
                tick={{ fontSize: 11 }}
                className="font-mono"
                tickFormatter={(k: number) => `k=${k}`}
              />
              <YAxis
                domain={[0, 1]}
                tick={{ fontSize: 11 }}
                tickFormatter={(v: number) => `${Math.round(v * 100)}%`}
                className="font-mono"
              />
              <Tooltip content={renderTooltip} />
              {visible.map((agent) => (
                <Line
                  key={agent.equivalence_key}
                  type="monotone"
                  dataKey={agent.equivalence_key}
                  stroke={colorByAgent[agent.equivalence_key]}
                  strokeWidth={2}
                  dot={{ r: 3 }}
                  activeDot={{ r: 5 }}
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </div>
        <div className="flex flex-wrap gap-2 pt-1">
          {agents.map((agent) => {
            const hidden = hiddenAgents.has(agent.equivalence_key);
            const color = colorByAgent[agent.equivalence_key];
            return (
              <button
                key={agent.equivalence_key}
                onClick={() => onToggleAgent(agent.equivalence_key)}
                className={`flex items-center gap-1.5 rounded-sm px-1.5 py-0.5 font-mono text-[11px] transition ${
                  hidden ? "opacity-40 hover:opacity-60" : "hover:bg-muted/50"
                }`}
                title={hidden ? "Click to show" : "Click to hide"}
              >
                <span
                  className="h-2.5 w-2.5 rounded-sm"
                  style={{
                    backgroundColor: hidden ? "transparent" : color,
                    border: `2px solid ${color}`,
                  }}
                />
                <span className={hidden ? "line-through" : ""}>
                  {formatAgent(agent)}
                </span>
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}

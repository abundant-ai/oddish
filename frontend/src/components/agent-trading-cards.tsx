"use client";

import { memo, useMemo } from "react";
import type { Task } from "@/lib/types";
import type { ExperimentAgentSummary } from "@/lib/experiment-agent-grouping";
import {
  buildAgentParetoPoints,
  paretoFrontier,
  type AgentParetoPoint,
} from "@/lib/pareto";
import {
  costEstimateMarks,
  formatCostUsd,
  formatDurationSec,
  formatTokenCount,
} from "@/lib/format";
import { AGENT_COLORS } from "./pass-at-k-graph";
import { QueueKeyIcon } from "./queue-key-icon";

interface AgentTradingCardsProps {
  tasks: Task[];
  agentSummaries: ExperimentAgentSummary[];
  hiddenAgents: Set<string>;
}

function StatRow({ name, value }: { name: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2 border-t border-dashed border-[color:var(--paper-line-2)] py-1 first:border-t-0">
      <span className="font-mono text-[9.5px] tracking-[0.06em] text-[color:var(--paper-ink-3)] uppercase">
        {name}
      </span>
      <span className="font-mono text-[11px] font-semibold text-[color:var(--paper-ink)]">
        {value}
      </span>
    </div>
  );
}

// One playful trading-card per agent — same cohorts, colors, and stats as
// the serious cards, just wearing a collectible frame.
export const AgentTradingCards = memo(function AgentTradingCards({
  tasks,
  agentSummaries,
  hiddenAgents,
}: AgentTradingCardsProps) {
  const { cards, frontierKeys } = useMemo(() => {
    const colorByKey = new Map(
      agentSummaries.map((summary, i) => [
        summary.key,
        AGENT_COLORS[i % AGENT_COLORS.length],
      ])
    );
    const points = buildAgentParetoPoints(tasks, agentSummaries).filter(
      (point) => !hiddenAgents.has(point.key)
    );
    const priced = points.filter((point) => point.metrics.cost != null);
    return {
      cards: points.map((point) => ({
        point,
        summary: agentSummaries.find((s) => s.key === point.key)!,
        color: colorByKey.get(point.key) ?? AGENT_COLORS[0],
      })),
      // The cost frontier decides who gets the holo star.
      frontierKeys: new Set(
        paretoFrontier(
          priced,
          (p) => p.metrics.cost!.value,
          (p) => p.score
        ).map((point) => point.key)
      ),
    };
  }, [tasks, agentSummaries, hiddenAgents]);

  if (cards.length === 0) {
    return null;
  }

  const statValue = (
    point: AgentParetoPoint,
    metric: "cost" | "costPerSuccess",
    format: (v: number) => string
  ): string => {
    const aggregate = point.metrics[metric];
    if (aggregate == null) return "—";
    const marks = costEstimateMarks(
      point.costHasEstimated,
      point.costHasNative
    );
    return `${marks.prefix}${format(aggregate.value)}${marks.suffix}`;
  };

  return (
    <div className="grid [grid-template-columns:repeat(auto-fill,minmax(180px,1fr))] gap-4">
      {cards.map(({ point, summary, color }) => (
        <div
          key={point.key}
          className="flex flex-col rounded-[12px] border-2 p-2.5"
          style={{
            borderColor: color,
            background: `linear-gradient(160deg, color-mix(in oklch, ${color} 14%, var(--paper-surface)) 0%, var(--paper-surface) 55%)`,
          }}
        >
          <div className="flex items-baseline justify-between gap-2">
            <span
              title={point.label}
              className="font-display truncate text-[13px] font-semibold tracking-[-0.01em] text-[color:var(--paper-ink)]"
            >
              {point.label}
            </span>
            <span className="font-mono text-[10px] font-bold whitespace-nowrap text-[color:var(--paper-ink-2)]">
              HP {Math.round(point.score * 100)}
            </span>
          </div>
          <div className="mt-1 h-1 overflow-hidden rounded-full bg-[color:var(--paper-bg-2)]">
            <div
              className="h-full rounded-full"
              style={{ width: `${point.score * 100}%`, background: color }}
            />
          </div>

          <div
            className="my-2.5 flex h-14 items-center justify-center rounded-[8px] border border-[color:var(--paper-line-2)]"
            style={{
              background: `radial-gradient(circle at 50% 40%, color-mix(in oklch, ${color} 22%, transparent) 0%, transparent 70%)`,
            }}
          >
            <QueueKeyIcon
              queueKey={summary.queueKey}
              model={summary.model}
              agent={summary.agent}
              size={28}
            />
          </div>

          <StatRow
            name="$ / trial"
            value={statValue(point, "cost", formatCostUsd)}
          />
          <StatRow
            name="$ / success"
            value={statValue(point, "costPerSuccess", formatCostUsd)}
          />
          <StatRow
            name="tokens"
            value={
              point.metrics.tokens
                ? formatTokenCount(point.metrics.tokens.value)
                : "—"
            }
          />
          <StatRow
            name="time"
            value={
              point.metrics.time
                ? formatDurationSec(point.metrics.time.value)
                : "—"
            }
          />

          <div className="mt-2 flex items-center justify-between border-t border-[color:var(--paper-line-2)] pt-1.5 font-mono text-[9px] text-[color:var(--paper-ink-3)]">
            <span>
              {point.taskCount} tasks · {point.trialCount} trials
            </span>
            {frontierKeys.has(point.key) && (
              <span title="On the cost frontier" style={{ color }}>
                ★ frontier
              </span>
            )}
          </div>
        </div>
      ))}
    </div>
  );
});

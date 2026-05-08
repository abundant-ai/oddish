"use client";

import { useMemo } from "react";
import { calculatePassAtK } from "@/lib/pass-at-k";
import { colorForAgent } from "@/components/experiment-pass-at-k";
import type {
  ExperimentCellAgent,
  ResolvedExperiment,
  ResolvedExperimentCell,
} from "@/lib/types";

interface Props {
  data: ResolvedExperiment;
}

interface Row {
  agent: ExperimentCellAgent;
  pass1: number;
  passBest: number;
}

function formatAgent(a: ExperimentCellAgent): string {
  const model = a.model && a.model !== "default" ? ` · ${a.model}` : "";
  return `${a.harness}${model}`;
}

export function ExperimentLeaderboard({ data }: Props) {
  const rows = useMemo<Row[]>(() => {
    const cellsByAgent = new Map<string, ResolvedExperimentCell[]>();
    for (const c of data.cells) {
      const list = cellsByAgent.get(c.agent.equivalence_key) ?? [];
      list.push(c);
      cellsByAgent.set(c.agent.equivalence_key, list);
    }
    const out: Row[] = [];
    for (const a of data.agents) {
      const list = (cellsByAgent.get(a.equivalence_key) ?? []).filter(
        (c) => c.have_n_total > 0,
      );
      if (list.length === 0) continue;
      // pass@1 averaged across tasks (binary, with multi-attempt
      // smoothing via the unbiased estimator at k=1).
      const pass1 =
        list.reduce(
          (acc, c) =>
            acc + calculatePassAtK(c.have_n_total, c.have_n_successful, 1),
          0,
        ) / list.length;
      // best-of-n: did the agent get *any* successful attempt on the
      // task? Averaged across tasks.
      const passBest =
        list.reduce((acc, c) => acc + (c.have_n_successful > 0 ? 1 : 0), 0) /
        list.length;
      out.push({
        agent: a,
        pass1,
        passBest,
      });
    }
    return out.sort((x, y) => y.pass1 - x.pass1);
  }, [data]);

  if (rows.length === 0) return null;

  return (
    <div className="rounded-sm border bg-card p-4">
      <h3 className="font-mono text-sm font-bold">Leaderboard</h3>
      <p className="mt-1 text-xs text-muted-foreground">
        pass@1 = expected single-attempt pass rate. best-of-n = fraction of
        tasks the agent passed at least once.
      </p>
      <div className="mt-3 overflow-x-auto">
        <table className="w-full font-mono text-xs">
          <thead>
            <tr className="text-left text-muted-foreground">
              <th className="py-1 pr-3 font-medium">Agent</th>
              <th className="py-1 px-3 text-right font-medium">pass@1</th>
              <th className="py-1 pl-3 text-right font-medium">best-of-n</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const color = colorForAgent(r.agent.equivalence_key, data.agents);
              return (
                <tr key={r.agent.equivalence_key} className="border-t">
                  <td className="py-1 pr-3">
                    <span className="inline-flex items-center gap-1.5">
                      <span
                        className="h-2.5 w-2.5 rounded-sm"
                        style={{ backgroundColor: color }}
                      />
                      {formatAgent(r.agent)}
                    </span>
                  </td>
                  <td className="py-1 px-3 text-right font-semibold">
                    {(r.pass1 * 100).toFixed(1)}%
                  </td>
                  <td className="py-1 pl-3 text-right">
                    {(r.passBest * 100).toFixed(1)}%
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

"use client";

import type {
  PublicReport,
  Report,
  ReportCellResolved,
  ReportColumnResolved,
  ReportRowResolved,
  ReportTrialSummary,
} from "@/lib/types";
import { cn } from "@/lib/utils";

interface ReportLike {
  rows: ReportRowResolved[];
  columns: ReportColumnResolved[];
  cells: Array<
    Pick<ReportCellResolved, "row_idx" | "col_idx"> & {
      have?: number;
      need?: number;
      source?: "resolved" | "pinned";
      trials: Array<
        Pick<
          ReportTrialSummary,
          "id" | "agent" | "model" | "status" | "reward" | "cost_usd"
        >
      >;
    }
  >;
}

interface ReportGridProps {
  report: ReportLike;
  /** When set, clicking a trial chip invokes this. */
  onTrialClick?: (
    trialId: string,
    row: ReportRowResolved,
    col: ReportColumnResolved,
  ) => void;
}

/** Pure-display cartesian grid for a report: rows = task_versions, cols = agents. */
export function ReportGrid({ report, onTrialClick }: ReportGridProps) {
  const cells = new Map<string, ReportLike["cells"][number]>();
  for (const cell of report.cells) {
    cells.set(`${cell.row_idx}:${cell.col_idx}`, cell);
  }

  return (
    <div className="overflow-x-auto rounded-md border">
      <table className="min-w-full text-sm">
        <thead className="bg-muted/40">
          <tr>
            <th className="sticky left-0 z-10 border-r bg-muted/40 px-3 py-2 text-left font-medium">
              Task / Version
            </th>
            {report.columns.map((col) => (
              <th
                key={col.column_key}
                className="border-r px-3 py-2 text-left font-medium"
              >
                <div className="flex flex-col leading-tight">
                  <span>{col.agent}</span>
                  {col.model ? (
                    <span className="text-xs text-muted-foreground">
                      {col.model}
                    </span>
                  ) : null}
                </div>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {report.rows.map((row) => (
            <tr key={row.task_version_id} className="border-t">
              <td className="sticky left-0 z-10 border-r bg-background px-3 py-2 align-top">
                <div className="flex flex-col leading-tight">
                  <span className="font-medium">{row.task_name}</span>
                  <span className="text-xs text-muted-foreground">
                    v{row.version} · {row.task_version_id}
                  </span>
                </div>
              </td>
              {report.columns.map((col) => {
                const cell = cells.get(`${row.position}:${col.position}`);
                return (
                  <td
                    key={col.column_key}
                    className="border-r px-3 py-2 align-top"
                  >
                    <CellContent
                      cell={cell}
                      onTrialClick={
                        onTrialClick
                          ? (tid) => onTrialClick(tid, row, col)
                          : undefined
                      }
                    />
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CellContent({
  cell,
  onTrialClick,
}: {
  cell: ReportLike["cells"][number] | undefined;
  onTrialClick?: (trialId: string) => void;
}) {
  if (!cell) return <span className="text-muted-foreground">—</span>;

  const trials = cell.trials ?? [];
  const missing = cell.need ?? 0;
  const pinned = cell.source === "pinned";

  if (trials.length === 0 && missing === 0) {
    return <span className="text-muted-foreground">—</span>;
  }

  return (
    <div className="flex flex-wrap items-center gap-1">
      {pinned ? (
        <span className="rounded bg-blue-500/10 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-blue-400">
          pinned
        </span>
      ) : null}
      {trials.map((trial) => (
        <TrialChip
          key={trial.id}
          trial={trial}
          onClick={onTrialClick ? () => onTrialClick(trial.id) : undefined}
        />
      ))}
      {missing > 0 ? (
        <span className="rounded bg-yellow-500/10 px-1.5 py-0.5 text-xs text-yellow-400">
          +{missing} missing
        </span>
      ) : null}
    </div>
  );
}

function TrialChip({
  trial,
  onClick,
}: {
  trial: ReportLike["cells"][number]["trials"][number];
  onClick?: () => void;
}) {
  const tone = rewardTone(trial.reward, trial.status);
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={!onClick}
      title={`${trial.agent}${trial.model ? `/${trial.model}` : ""} · ${trial.status} · reward=${trial.reward ?? "—"}`}
      className={cn(
        "rounded border px-1.5 py-0.5 text-xs",
        tone,
        onClick ? "cursor-pointer hover:bg-muted" : "cursor-default",
      )}
    >
      {formatReward(trial.reward, trial.status)}
    </button>
  );
}

function rewardTone(
  reward: number | null,
  status: string,
): string {
  if (status === "failed") return "border-red-500/40 text-red-300";
  if (reward === null || reward === undefined) {
    return "border-muted-foreground/40 text-muted-foreground";
  }
  if (reward >= 1) return "border-green-500/40 text-green-300";
  if (reward <= 0) return "border-red-500/40 text-red-300";
  return "border-yellow-500/40 text-yellow-200";
}

function formatReward(reward: number | null, status: string): string {
  if (status === "failed") return "✗";
  if (reward === null || reward === undefined) return "—";
  if (reward === 1) return "✓";
  if (reward === 0) return "✗";
  return reward.toFixed(2);
}

// Helper to cast a Report (authed) or PublicReport (anon) into the loose
// ReportLike shape. Lets the same grid render both surfaces.
export function asReportLike(r: Report | PublicReport): ReportLike {
  return r as unknown as ReportLike;
}

"use client";

import { useState } from "react";
import useSWR from "swr";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { fetcher } from "@/lib/api";
import type {
  BackfillCellPlan,
  BackfillExecuteResponse,
  BackfillPlan,
} from "@/lib/types";

interface AggregatedColumnRow {
  column_key: string;
  agent: string;
  model: string | null;
  task_count: number;
  need_total: number;
  est_cost_total: number | null;
  sample_min: number;
  sample_max: number;
}

/** Collapse per-cell plan rows into one row per agent column.
 *
 * The plan API returns ``(task, agent)`` cells, but for backfill
 * decision-making the unit is the column — "how many trials of agent
 * X do I owe, and what will they cost in aggregate." Summing here
 * keeps the table linear in agents (~7 rows) instead of linear in
 * cells (~14+) without losing information; the breakdown by task is
 * still in the spec.
 */
function aggregateByColumn(cells: BackfillCellPlan[]): AggregatedColumnRow[] {
  const byKey = new Map<string, AggregatedColumnRow>();
  for (const cell of cells) {
    const existing = byKey.get(cell.column_key);
    const cost = cell.est_cost_usd;
    if (!existing) {
      byKey.set(cell.column_key, {
        column_key: cell.column_key,
        agent: cell.agent,
        model: cell.model,
        task_count: 1,
        need_total: cell.need,
        est_cost_total: cost,
        sample_min: cell.cost_sample_size,
        sample_max: cell.cost_sample_size,
      });
      continue;
    }
    existing.task_count += 1;
    existing.need_total += cell.need;
    if (cost !== null) {
      existing.est_cost_total =
        existing.est_cost_total === null ? cost : existing.est_cost_total + cost;
    }
    existing.sample_min = Math.min(existing.sample_min, cell.cost_sample_size);
    existing.sample_max = Math.max(existing.sample_max, cell.cost_sample_size);
  }
  return Array.from(byKey.values()).sort(
    (a, b) => b.need_total - a.need_total,
  );
}

interface Props {
  reportId: string;
  onSubmitted?: (resp: BackfillExecuteResponse) => void;
}

/** Banner shown above the report grid when cells are short of trials_per_cell. */
export function ReportBackfillBanner({ reportId, onSubmitted }: Props) {
  const [dialogOpen, setDialogOpen] = useState(false);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { data: plan, mutate } = useSWR<BackfillPlan>(
    `/api/reports/${reportId}/backfill/plan`,
    fetcher,
    { refreshInterval: 10_000 },
  );

  if (!plan || plan.total_missing === 0) return null;

  const est = plan.total_est_cost_usd;
  const lowSample = plan.total_cost_sample_size < 5;
  const blocked = plan.columns_missing_backfill_config.length > 0;

  async function execute() {
    setSubmitting(true);
    setError(null);
    try {
      const res = await fetch(
        `/api/reports/${reportId}/backfill/execute`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify({ confirm: true, source: "web" }),
        },
      );
      const body = (await res.json()) as BackfillExecuteResponse | {
        detail?: unknown;
      };
      if (!res.ok) {
        setError(
          typeof (body as { detail?: unknown }).detail === "string"
            ? ((body as { detail: string }).detail)
            : JSON.stringify(body),
        );
        return;
      }
      onSubmitted?.(body as BackfillExecuteResponse);
      setDialogOpen(false);
      void mutate();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to submit");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="rounded-md border border-yellow-500/30 bg-yellow-500/5 p-3 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="space-y-1">
          <div className="font-medium">
            {plan.cells.filter((c) => c.need > 0).length} cells short by{" "}
            {plan.total_missing} trials
            {est !== null ? (
              <>
                {" "}
                · est.{" "}
                <span className={lowSample ? "text-yellow-300" : undefined}>
                  {lowSample ? "~" : ""}${est.toFixed(2)}
                  {lowSample ? " (low sample)" : null}
                </span>
              </>
            ) : (
              " · est. cost unavailable"
            )}
          </div>
          {blocked ? (
            <div className="text-xs text-red-300">
              Add a `backfill` block in the spec for:{" "}
              {plan.columns_missing_backfill_config.join(", ")}
            </div>
          ) : null}
        </div>
        <div className="flex gap-2">
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setPreviewOpen((v) => !v)}
          >
            {previewOpen ? "Hide gap" : "Preview gap"}
          </Button>
          <Button
            size="sm"
            disabled={blocked}
            onClick={() => setDialogOpen(true)}
          >
            Run backfill
          </Button>
        </div>
      </div>

      {previewOpen ? (
        <div className="mt-3 max-h-64 overflow-auto rounded border border-yellow-500/20 bg-background/60">
          <table className="min-w-full text-xs">
            <thead className="bg-muted/40 text-left">
              <tr>
                <th className="px-2 py-1">Agent</th>
                <th className="px-2 py-1" title="Tasks this column is short on">
                  Tasks
                </th>
                <th className="px-2 py-1">Need</th>
                <th className="px-2 py-1">Est $</th>
                <th className="px-2 py-1">Sample n</th>
              </tr>
            </thead>
            <tbody>
              {aggregateByColumn(plan.cells).map((row) => (
                <tr key={row.column_key} className="border-t">
                  <td className="px-2 py-1">
                    {row.column_key}
                    <span className="ml-1 text-muted-foreground">
                      ({row.agent}
                      {row.model ? `/${row.model}` : ""})
                    </span>
                  </td>
                  <td className="px-2 py-1">{row.task_count}</td>
                  <td className="px-2 py-1">{row.need_total}</td>
                  <td className="px-2 py-1">
                    {row.est_cost_total !== null
                      ? `$${row.est_cost_total.toFixed(2)}`
                      : "?"}
                  </td>
                  <td
                    className={
                      row.sample_min > 0 && row.sample_min < 5
                        ? "px-2 py-1 text-yellow-300"
                        : "px-2 py-1"
                    }
                    title={
                      row.sample_min === row.sample_max
                        ? `${row.sample_min} historical trials`
                        : `${row.sample_min}–${row.sample_max} historical trials per task`
                    }
                  >
                    {row.sample_min === row.sample_max
                      ? row.sample_min
                      : `${row.sample_min}–${row.sample_max}`}
                  </td>
                </tr>
              ))}
            </tbody>
            <tfoot className="border-t bg-muted/40 font-medium">
              <tr>
                <td className="px-2 py-1" colSpan={2}>
                  Total
                </td>
                <td className="px-2 py-1">
                  {plan.cells.reduce((acc, c) => acc + c.need, 0)}
                </td>
                <td className="px-2 py-1">
                  {plan.total_est_cost_usd !== null
                    ? `${lowSample ? "~" : ""}$${plan.total_est_cost_usd.toFixed(2)}`
                    : "?"}
                </td>
                <td className="px-2 py-1">
                  {plan.total_cost_sample_size}
                </td>
              </tr>
            </tfoot>
          </table>
        </div>
      ) : null}

      <AlertDialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Run backfill?</AlertDialogTitle>
            <AlertDialogDescription>
              This will queue {plan.total_missing} trials across{" "}
              {plan.cells.filter((c) => c.need > 0).length} cells. Estimated
              cost:{" "}
              {est !== null ? (
                <strong>
                  {lowSample ? "~" : ""}${est.toFixed(2)}
                </strong>
              ) : (
                <em>unavailable — no historical trials for these pairs</em>
              )}
              {plan.total_cost_sample_size > 0
                ? ` based on ${plan.total_cost_sample_size} historical trials`
                : null}
              .
            </AlertDialogDescription>
          </AlertDialogHeader>
          {error ? (
            <p className="text-sm text-red-400">{error}</p>
          ) : null}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={submitting}>Cancel</AlertDialogCancel>
            <AlertDialogAction asChild>
              <Button onClick={execute} disabled={submitting}>
                {submitting ? (
                  <>
                    <Loader2 className="mr-2 h-3 w-3 animate-spin" />
                    Submitting…
                  </>
                ) : (
                  "Confirm"
                )}
              </Button>
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

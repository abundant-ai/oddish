"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import useSWR from "swr";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { fetcher } from "@/lib/api";
import { encodeExperimentRouteParam, formatRelativeTime } from "@/lib/utils";
import type {
  ExperimentBackfillResponse,
  ResolvedExperimentCell,
} from "@/lib/types";
import { Loader2, Pencil, Play, Trash2 } from "lucide-react";
import { TrialInspectDrawer } from "@/components/trial-inspect-drawer";

type AgentColumn = {
  key: string;
  harness: string;
  model: string;
  provider: string;
};

type MatrixRow = {
  taskVersionId: string;
  taskId: string | null;
  taskName: string | null;
  version: number | null;
  cellsByAgent: Record<string, ResolvedExperimentCell | undefined>;
};

function formatMeanReward(value: number | null | undefined) {
  if (value == null) return "—";
  return `${Math.round(value * 100)}%`;
}

function buildMatrix(cells: ResolvedExperimentCell[]) {
  const agentsByKey = new Map<string, AgentColumn>();
  const rowsByVersion = new Map<string, MatrixRow>();

  for (const resolved of cells) {
    const cell = resolved.cell;
    agentsByKey.set(cell.agent_equivalence_key, {
      key: cell.agent_equivalence_key,
      harness: cell.harness,
      model: cell.model,
      provider: cell.provider,
    });

    const row = rowsByVersion.get(cell.task_version_id) ?? {
      taskVersionId: cell.task_version_id,
      taskId: resolved.task_id ?? cell.task_version_id.replace(/-v\d+$/, ""),
      taskName: resolved.task_name ?? null,
      version: resolved.task_version ?? null,
      cellsByAgent: {},
    };
    row.cellsByAgent[cell.agent_equivalence_key] = resolved;
    rowsByVersion.set(cell.task_version_id, row);
  }

  const agents = Array.from(agentsByKey.values()).sort((a, b) =>
    `${a.harness}|${a.provider}|${a.model}`.localeCompare(
      `${b.harness}|${b.provider}|${b.model}`
    )
  );
  const rows = Array.from(rowsByVersion.values()).sort((a, b) => {
    const aName = a.taskName ?? a.taskId ?? a.taskVersionId;
    const bName = b.taskName ?? b.taskId ?? b.taskVersionId;
    return aName.localeCompare(bName) || (a.version ?? 0) - (b.version ?? 0);
  });

  return { agents, rows };
}

function taskHref(row: MatrixRow) {
  if (!row.taskId) return "#";
  return `/tasks/${encodeURIComponent(row.taskId)}${
    row.version != null ? `?version=${row.version}` : ""
  }`;
}

export function ExperimentCellsPanel({
  experimentId,
}: {
  experimentId: string;
}) {
  const encodedId = encodeExperimentRouteParam(experimentId);
  const {
    data: cells = [],
    error,
    mutate,
  } = useSWR<ResolvedExperimentCell[]>(
    experimentId ? `/api/experiments/${encodedId}/cells` : null,
    fetcher,
    {
      refreshInterval: 30000,
      revalidateOnFocus: false,
    }
  );
  const [editingCellId, setEditingCellId] = useState<string | null>(null);
  const [targetDraft, setTargetDraft] = useState("");
  const [busyCellId, setBusyCellId] = useState<string | null>(null);
  const [isBackfilling, setIsBackfilling] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [inspecting, setInspecting] = useState<{
    taskId: string;
    trialId: string;
    siblingTrialIds: string[];
  } | null>(null);

  const matrix = useMemo(() => buildMatrix(cells), [cells]);
  const gapCount = useMemo(
    () =>
      cells.reduce(
        (sum, item) =>
          sum + Math.max(0, item.cell.target_n_trials - item.have_n_trials),
        0
      ),
    [cells]
  );

  async function updateTarget(cell: ResolvedExperimentCell) {
    const target = Math.max(0, Number(targetDraft || 0));
    setBusyCellId(cell.cell.id);
    setActionError(null);
    try {
      const res = await fetch(
        `/api/experiments/${encodedId}/cells/${encodeURIComponent(cell.cell.id)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ target_n_trials: target }),
        }
      );
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        throw new Error(data?.detail || data?.error || "Failed to update cell");
      }
      setEditingCellId(null);
      await mutate();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Cell update failed");
    } finally {
      setBusyCellId(null);
    }
  }

  async function deleteCell(cell: ResolvedExperimentCell) {
    setBusyCellId(cell.cell.id);
    setActionError(null);
    try {
      const res = await fetch(
        `/api/experiments/${encodedId}/cells/${encodeURIComponent(cell.cell.id)}`,
        { method: "DELETE" }
      );
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        throw new Error(data?.detail || data?.error || "Failed to delete cell");
      }
      await mutate();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Cell delete failed");
    } finally {
      setBusyCellId(null);
    }
  }

  async function backfillGaps() {
    setIsBackfilling(true);
    setMessage(null);
    setActionError(null);
    try {
      const res = await fetch(`/api/experiments/${encodedId}/backfill`, {
        method: "POST",
      });
      const data = (await res.json().catch(() => null)) as
        | ExperimentBackfillResponse
        | { detail?: string; error?: string }
        | null;
      if (!res.ok || !data || !("job_id" in data)) {
        const errorData = data && !("job_id" in data) ? data : null;
        throw new Error(
          errorData?.detail || errorData?.error || "Backfill failed"
        );
      }
      setMessage(
        data.enqueued_trials === 0
          ? "No gaps to backfill."
          : `Queued ${data.enqueued_trials} trial${
              data.enqueued_trials === 1 ? "" : "s"
            } in job ${data.job_id}.`
      );
      await mutate();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Backfill failed");
    } finally {
      setIsBackfilling(false);
    }
  }

  return (
    <Card className="border-[#6f88b4]/20 shadow-xs">
      <CardHeader className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <CardTitle className="text-base">Experiment Matrix</CardTitle>
          <p className="text-muted-foreground mt-1 text-xs">
            Rows are frozen task versions. Columns are agent identities. Cells
            resolve against pooled trial evidence.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="outline">{cells.length} cells</Badge>
          <Badge variant={gapCount > 0 ? "default" : "outline"}>
            {gapCount} gap{gapCount === 1 ? "" : "s"}
          </Badge>
          <Button
            type="button"
            size="sm"
            onClick={backfillGaps}
            disabled={isBackfilling || cells.length === 0}
          >
            {isBackfilling ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            ) : (
              <Play className="mr-2 h-4 w-4" />
            )}
            Backfill gaps
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {error ? (
          <Alert variant="destructive">
            <AlertTitle>Failed to load experiment cells</AlertTitle>
            <AlertDescription>
              The legacy task table below may still load.
            </AlertDescription>
          </Alert>
        ) : null}
        {actionError ? (
          <Alert variant="destructive">
            <AlertTitle>Experiment action failed</AlertTitle>
            <AlertDescription>{actionError}</AlertDescription>
          </Alert>
        ) : null}
        {message ? (
          <Alert>
            <AlertTitle>Experiment action complete</AlertTitle>
            <AlertDescription>{message}</AlertDescription>
          </Alert>
        ) : null}

        {cells.length === 0 ? (
          <div className="bg-card/60 text-muted-foreground rounded-lg border border-dashed border-[#6f88b4]/30 px-6 py-8 text-center text-sm">
            No cells are saved for this experiment yet.{" "}
            <Link
              href="/experiments/new"
              className="text-[#5d77a5] hover:underline dark:text-[#a8b8d2]"
            >
              Build a task-first experiment
            </Link>
            .
          </div>
        ) : (
          <div className="border-border/70 overflow-x-auto rounded-lg border">
            <table className="min-w-full border-collapse text-sm">
              <thead>
                <tr className="bg-muted/40 text-muted-foreground text-[11px] tracking-wide uppercase">
                  <th className="border-border/70 bg-muted sticky left-0 z-10 min-w-[220px] border-r px-3 py-2 text-left font-medium">
                    Task Version
                  </th>
                  {matrix.agents.map((agent) => (
                    <th
                      key={agent.key}
                      className="border-border/70 min-w-[230px] border-r px-3 py-2 text-left font-medium"
                    >
                      <div className="text-foreground font-mono">
                        {agent.harness}
                      </div>
                      <div className="text-muted-foreground truncate normal-case">
                        {agent.provider}/{agent.model}
                      </div>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {matrix.rows.map((row) => (
                  <tr
                    key={row.taskVersionId}
                    className="border-border/70 border-t"
                  >
                    <td className="border-border/70 bg-card sticky left-0 z-10 border-r px-3 py-3 align-top">
                      <Link
                        href={taskHref(row)}
                        className="font-mono text-[#5d77a5] underline-offset-4 hover:underline dark:text-[#a8b8d2]"
                      >
                        {row.taskName ?? row.taskId ?? row.taskVersionId}
                      </Link>
                      <div className="mt-1 flex flex-wrap items-center gap-1">
                        <Badge variant="outline">
                          {row.version != null ? `v${row.version}` : "version"}
                        </Badge>
                        <span className="text-muted-foreground font-mono text-[10px]">
                          {row.taskVersionId}
                        </span>
                      </div>
                    </td>
                    {matrix.agents.map((agent) => {
                      const cell = row.cellsByAgent[agent.key];
                      if (!cell) {
                        return (
                          <td
                            key={agent.key}
                            className="border-border/70 text-muted-foreground border-r px-3 py-3"
                          >
                            —
                          </td>
                        );
                      }

                      const gap = Math.max(
                        0,
                        cell.cell.target_n_trials - cell.have_n_trials
                      );
                      const isEditing = editingCellId === cell.cell.id;
                      return (
                        <td
                          key={agent.key}
                          className="border-border/70 border-r px-3 py-3 align-top"
                        >
                          <div className="flex items-start justify-between gap-2">
                            <div>
                              <div className="font-mono text-sm">
                                {cell.have_n_trials}/{cell.cell.target_n_trials}
                              </div>
                              <div className="text-muted-foreground mt-1 text-xs">
                                gap {gap} · mean{" "}
                                {formatMeanReward(cell.mean_reward)}
                              </div>
                              <div className="text-muted-foreground mt-1 text-xs">
                                {cell.last_run_at
                                  ? formatRelativeTime(cell.last_run_at)
                                  : "never run"}
                              </div>
                            </div>
                            <div className="flex items-center gap-1">
                              <Button
                                type="button"
                                variant="ghost"
                                size="icon"
                                className="h-7 w-7"
                                onClick={() => {
                                  setEditingCellId(cell.cell.id);
                                  setTargetDraft(
                                    String(cell.cell.target_n_trials)
                                  );
                                }}
                              >
                                <Pencil className="h-3.5 w-3.5" />
                              </Button>
                              <Button
                                type="button"
                                variant="ghost"
                                size="icon"
                                className="text-destructive h-7 w-7"
                                disabled={busyCellId === cell.cell.id}
                                onClick={() => void deleteCell(cell)}
                              >
                                <Trash2 className="h-3.5 w-3.5" />
                              </Button>
                            </div>
                          </div>

                          {isEditing ? (
                            <div className="mt-2 flex items-center gap-2">
                              <Input
                                type="number"
                                min={0}
                                value={targetDraft}
                                onChange={(event) =>
                                  setTargetDraft(event.target.value)
                                }
                                className="h-8 w-20"
                              />
                              <Button
                                type="button"
                                size="sm"
                                className="h-8"
                                disabled={busyCellId === cell.cell.id}
                                onClick={() => void updateTarget(cell)}
                              >
                                Save
                              </Button>
                              <Button
                                type="button"
                                variant="ghost"
                                size="sm"
                                className="h-8"
                                onClick={() => setEditingCellId(null)}
                              >
                                Cancel
                              </Button>
                            </div>
                          ) : null}

                          {cell.trial_ids.length > 0 && row.taskId ? (
                            <Button
                              type="button"
                              variant="outline"
                              size="sm"
                              className="mt-3 h-7"
                              onClick={() =>
                                setInspecting({
                                  taskId: row.taskId as string,
                                  trialId: cell.trial_ids[0],
                                  siblingTrialIds: cell.trial_ids,
                                })
                              }
                            >
                              Inspect trials
                            </Button>
                          ) : null}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>

      {inspecting ? (
        <TrialInspectDrawer
          open
          onOpenChange={(open) => {
            if (!open) setInspecting(null);
          }}
          taskId={inspecting.taskId}
          trialId={inspecting.trialId}
          siblingTrialIds={inspecting.siblingTrialIds}
          onTrialChange={(trialId) =>
            setInspecting((current) =>
              current ? { ...current, trialId } : current
            )
          }
        />
      ) : null}
    </Card>
  );
}

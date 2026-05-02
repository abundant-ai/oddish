"use client";

import { useMemo, useState } from "react";
import useSWR from "swr";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { TrialInspectDrawer } from "@/components/trial-inspect-drawer";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { Plus, Pencil, Trash2 } from "lucide-react";
import { fetcher } from "@/lib/api";
import type {
  ExperimentCellAgent,
  ResolvedExperiment,
  ResolvedExperimentCell,
} from "@/lib/types";
import { encodeExperimentRouteParam } from "@/lib/utils";

type Props = {
  experimentId: string;
  canEdit: boolean;
};

type Pivot = {
  agents: { key: string; agent: ExperimentCellAgent }[];
  rows: {
    taskVersionId: string;
    taskId: string;
    taskName: string | null;
    version: number | null;
    cellsByAgent: Record<string, ResolvedExperimentCell | undefined>;
  }[];
};

function pivotCells(cells: ResolvedExperimentCell[]): Pivot {
  const agentByKey = new Map<string, ExperimentCellAgent>();
  const rowsByTV = new Map<string, Pivot["rows"][number]>();

  for (const cell of cells) {
    agentByKey.set(cell.agent.equivalence_key, cell.agent);
    const row = rowsByTV.get(cell.task_version_id) ?? {
      taskVersionId: cell.task_version_id,
      taskId: cell.task_id,
      taskName: cell.task_name,
      version: cell.task_version,
      cellsByAgent: {},
    };
    row.cellsByAgent[cell.agent.equivalence_key] = cell;
    rowsByTV.set(cell.task_version_id, row);
  }

  const agents = Array.from(agentByKey.entries())
    .sort((a, b) => {
      const an = `${a[1].harness}|${a[1].model ?? ""}`;
      const bn = `${b[1].harness}|${b[1].model ?? ""}`;
      return an.localeCompare(bn);
    })
    .map(([key, agent]) => ({ key, agent }));

  const rows = Array.from(rowsByTV.values()).sort((a, b) => {
    const an = a.taskName ?? a.taskId;
    const bn = b.taskName ?? b.taskId;
    return an.localeCompare(bn) || (a.version ?? 0) - (b.version ?? 0);
  });

  return { agents, rows };
}

function formatAgentLabel(agent: ExperimentCellAgent): string {
  const model = agent.model ? ` · ${agent.model}` : "";
  return `${agent.harness}${model}`;
}

function formatReward(reward: number | null | undefined): string {
  if (reward === null || reward === undefined) return "—";
  return reward.toFixed(2);
}

interface CellTrial {
  id: string;
  status: string;
  reward: number | null;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string | null;
  task_id: string;
  agent: string;
  model: string | null;
  provider: string;
}

function CellTrialsDialog({
  experimentId,
  cellId,
  open,
  onOpenChange,
  agent,
}: {
  experimentId: string;
  cellId: string;
  open: boolean;
  onOpenChange: (o: boolean) => void;
  agent: ExperimentCellAgent;
}) {
  const url =
    open && cellId
      ? `/api/experiments/${encodeExperimentRouteParam(experimentId)}/cells/${encodeURIComponent(cellId)}/trials`
      : null;
  const { data, error, isLoading } = useSWR<CellTrial[]>(url, fetcher);

  const [inspecting, setInspecting] = useState<{
    trialId: string;
    taskId: string;
  } | null>(null);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>
            Trials · {agent.harness}
            {agent.model ? ` · ${agent.model}` : ""}
          </DialogTitle>
        </DialogHeader>
        {error ? (
          <div className="rounded-md border border-destructive/40 bg-destructive/5 p-2 text-xs">
            {String((error as Error).message)}
          </div>
        ) : isLoading || !data ? (
          <Skeleton className="h-40" />
        ) : data.length === 0 ? (
          <div className="rounded-md border border-dashed p-6 text-center text-sm text-muted-foreground">
            No trials match this cell yet.
          </div>
        ) : (
          <div className="max-h-[60vh] overflow-y-auto rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Trial</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Reward</TableHead>
                  <TableHead>Finished</TableHead>
                  <TableHead>Error</TableHead>
                  <TableHead>Inspect</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.map((t) => (
                  <TableRow key={t.id}>
                    <TableCell className="font-mono text-[11px]">
                      {t.id}
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant={
                          t.status === "success"
                            ? "secondary"
                            : t.status === "failed"
                              ? "destructive"
                              : "outline"
                        }
                      >
                        {t.status}
                      </Badge>
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      {t.reward === null ? "—" : t.reward.toFixed(2)}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {t.finished_at
                        ? new Date(t.finished_at).toLocaleString()
                        : "—"}
                    </TableCell>
                    <TableCell className="max-w-[28ch] truncate text-xs text-rose-600 dark:text-rose-400">
                      {t.error_message ?? ""}
                    </TableCell>
                    <TableCell className="whitespace-nowrap text-[11px]">
                      <Button
                        size="sm"
                        variant="outline"
                        className="h-7"
                        onClick={() =>
                          setInspecting({ trialId: t.id, taskId: t.task_id })
                        }
                      >
                        Inspect
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </DialogContent>
      {inspecting ? (
        <TrialInspectDrawer
          open={true}
          onOpenChange={(o) => {
            if (!o) setInspecting(null);
          }}
          trialId={inspecting.trialId}
          taskId={inspecting.taskId}
          siblingTrialIds={(data ?? [])
            .filter((t) => t.task_id === inspecting.taskId)
            .map((t) => t.id)}
          onTrialChange={(nextId) =>
            setInspecting({ trialId: nextId, taskId: inspecting.taskId })
          }
        />
      ) : null}
    </Dialog>
  );
}

function CellBadge({
  cell,
  canEdit,
  experimentId,
  onUpdateTarget,
  onDelete,
}: {
  cell: ResolvedExperimentCell | undefined;
  canEdit: boolean;
  experimentId: string;
  onUpdateTarget: (cellId: string, target: number) => Promise<void>;
  onDelete: (cellId: string) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<string>(
    cell ? String(cell.target_n_trials) : "1",
  );
  const [trialsOpen, setTrialsOpen] = useState(false);

  if (!cell) {
    return <span className="text-muted-foreground text-xs">—</span>;
  }
  const ratioColor =
    cell.gap === 0
      ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300"
      : cell.have_n_running > 0
        ? "bg-amber-500/15 text-amber-700 dark:text-amber-300"
        : "bg-rose-500/15 text-rose-700 dark:text-rose-300";

  const badge = (
    <div
      className={`inline-flex flex-col items-start gap-0.5 rounded-md px-2 py-1 ${ratioColor}`}
    >
      <span className="font-mono text-xs">
        {cell.have_n_successful}/{cell.target_n_trials}
      </span>
      <span className="text-[10px] opacity-80">
        μ {formatReward(cell.mean_reward)}
      </span>
    </div>
  );

  if (!canEdit) {
    return (
      <>
        <TooltipProvider delayDuration={150}>
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                type="button"
                className="inline-flex"
                onClick={() => setTrialsOpen(true)}
                aria-label="View trials"
              >
                {badge}
              </button>
            </TooltipTrigger>
            <TooltipContent side="top" className="max-w-xs space-y-1 text-xs">
              <div className="font-mono">
                successful: {cell.have_n_successful} · failed:{" "}
                {cell.have_n_failed} · running: {cell.have_n_running}
              </div>
              <div>
                target: {cell.target_n_trials} · gap:{" "}
                <span className="font-semibold">{cell.gap}</span>
              </div>
              {cell.last_run_at ? (
                <div className="text-muted-foreground">
                  last run {new Date(cell.last_run_at).toLocaleString()}
                </div>
              ) : null}
              <div className="text-[10px] text-muted-foreground">
                click to view trials
              </div>
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>
        <CellTrialsDialog
          experimentId={experimentId}
          cellId={cell.id}
          open={trialsOpen}
          onOpenChange={setTrialsOpen}
          agent={cell.agent}
        />
      </>
    );
  }

  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="inline-flex"
          aria-label="Edit cell"
        >
          {badge}
        </button>
      </PopoverTrigger>
      <PopoverContent className="w-72 space-y-2 p-3 text-xs">
        <div className="font-mono">
          successful: {cell.have_n_successful} · failed: {cell.have_n_failed} ·
          running: {cell.have_n_running}
        </div>
        <div>
          target: {cell.target_n_trials} · gap:{" "}
          <span className="font-semibold">{cell.gap}</span>
        </div>
        {cell.last_run_at ? (
          <div className="text-muted-foreground">
            last run {new Date(cell.last_run_at).toLocaleString()}
          </div>
        ) : null}
        {editing ? (
          <div className="flex items-center gap-2">
            <Input
              type="number"
              min={1}
              className="h-7 w-20 text-xs"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
            />
            <Button
              size="sm"
              className="h-7"
              onClick={async () => {
                const n = Math.max(1, parseInt(draft, 10) || 1);
                await onUpdateTarget(cell.id, n);
                setEditing(false);
              }}
            >
              Save
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="h-7"
              onClick={() => {
                setDraft(String(cell.target_n_trials));
                setEditing(false);
              }}
            >
              Cancel
            </Button>
          </div>
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              variant="default"
              className="h-7"
              onClick={() => setTrialsOpen(true)}
            >
              View {cell.have_n_total} trial
              {cell.have_n_total === 1 ? "" : "s"}
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="h-7"
              onClick={() => setEditing(true)}
            >
              <Pencil className="mr-1 h-3 w-3" /> Bump target
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="h-7 text-rose-600 hover:text-rose-700"
              onClick={() => onDelete(cell.id)}
            >
              <Trash2 className="mr-1 h-3 w-3" /> Remove
            </Button>
          </div>
        )}
      </PopoverContent>
      <CellTrialsDialog
        experimentId={experimentId}
        cellId={cell.id}
        open={trialsOpen}
        onOpenChange={setTrialsOpen}
        agent={cell.agent}
      />
    </Popover>
  );
}

interface BrowseTask {
  id: string;
  name: string;
  current_version: number | null;
  current_version_id: string | null;
}

interface BrowseResponse {
  items: BrowseTask[];
  has_more: boolean;
}

function AddCellDialog({
  experimentId,
  onCreated,
}: {
  experimentId: string;
  onCreated: () => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [taskId, setTaskId] = useState<string>("");
  const [harness, setHarness] = useState("claude-code");
  const [model, setModel] = useState("claude-sonnet-4-5");
  const [provider, setProvider] = useState("anthropic");
  const [targetN, setTargetN] = useState("3");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const { data: browse } = useSWR<BrowseResponse>(
    open ? "/api/tasks/browse?limit=200&offset=0" : null,
    fetcher,
  );

  const tasks = useMemo(() => {
    return (browse?.items ?? []).filter((t) => t.current_version_id !== null);
  }, [browse]);

  const selectedTask = tasks.find((t) => t.id === taskId);

  const reset = () => {
    setTaskId("");
    setHarness("claude-code");
    setModel("claude-sonnet-4-5");
    setProvider("anthropic");
    setTargetN("3");
    setError(null);
  };

  const onSubmit = async () => {
    setError(null);
    if (!selectedTask?.current_version_id) {
      setError("Pick a task with at least one uploaded version");
      return;
    }
    if (!harness.trim() || !provider.trim()) {
      setError("Harness and provider are required");
      return;
    }
    setSubmitting(true);
    try {
      const res = await fetch(
        `/api/experiments/${encodeExperimentRouteParam(experimentId)}/cells`,
        {
          method: "POST",
          credentials: "include",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            task_version_id: selectedTask.current_version_id,
            agent_harness: harness.trim(),
            agent_model: model.trim() || null,
            agent_provider: provider.trim(),
            target_n_trials: Math.max(1, parseInt(targetN, 10) || 1),
          }),
        },
      );
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || `Failed (${res.status})`);
      }
      await onCreated();
      reset();
      setOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        setOpen(o);
        if (!o) reset();
      }}
    >
      <DialogTrigger asChild>
        <Button size="sm" variant="outline">
          <Plus className="mr-1 h-3.5 w-3.5" /> Add cell
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add cell to experiment</DialogTitle>
        </DialogHeader>
        <div className="space-y-3 text-sm">
          <div>
            <Label htmlFor="task">Task</Label>
            <Select value={taskId} onValueChange={setTaskId}>
              <SelectTrigger id="task">
                <SelectValue
                  placeholder={
                    browse ? "Select a task" : "Loading tasks…"
                  }
                />
              </SelectTrigger>
              <SelectContent className="max-h-[40vh]">
                {tasks.map((t) => (
                  <SelectItem key={t.id} value={t.id}>
                    {t.name}{" "}
                    <span className="text-muted-foreground">
                      v{t.current_version ?? "?"}
                    </span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {selectedTask ? (
              <p className="mt-1 font-mono text-[10px] text-muted-foreground">
                pinning to {selectedTask.current_version_id}
              </p>
            ) : (
              <p className="mt-1 text-xs text-muted-foreground">
                Cells pin to a specific task version. Re-uploading the
                task later does not shift this cell's evidence pool.
              </p>
            )}
          </div>
          <div className="grid grid-cols-3 gap-2">
            <div className="col-span-1">
              <Label htmlFor="harness">Harness</Label>
              <Input
                id="harness"
                placeholder="claude-code"
                value={harness}
                onChange={(e) => setHarness(e.target.value)}
              />
            </div>
            <div className="col-span-2">
              <Label htmlFor="model">Model</Label>
              <Input
                id="model"
                placeholder="claude-sonnet-4-5"
                value={model}
                onChange={(e) => setModel(e.target.value)}
              />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <div>
              <Label htmlFor="provider">Provider</Label>
              <Input
                id="provider"
                placeholder="anthropic"
                value={provider}
                onChange={(e) => setProvider(e.target.value)}
              />
            </div>
            <div>
              <Label htmlFor="target_n_trials">Target trials</Label>
              <Input
                id="target_n_trials"
                type="number"
                min={1}
                value={targetN}
                onChange={(e) => setTargetN(e.target.value)}
              />
            </div>
          </div>
          {error ? (
            <div className="rounded-md border border-destructive/40 bg-destructive/5 p-2 text-xs">
              {error}
            </div>
          ) : null}
        </div>
        <DialogFooter>
          <Button
            variant="ghost"
            onClick={() => {
              reset();
              setOpen(false);
            }}
            disabled={submitting}
          >
            Cancel
          </Button>
          <Button onClick={onSubmit} disabled={submitting}>
            {submitting ? "Adding…" : "Add cell"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function ExperimentCellMatrix({ experimentId, canEdit }: Props) {
  const encodedId = encodeExperimentRouteParam(experimentId);
  const url = experimentId
    ? `/api/experiments/${encodedId}/resolved`
    : null;

  const {
    data,
    error,
    isLoading,
    mutate,
  } = useSWR<ResolvedExperiment>(url, fetcher, {
    refreshInterval: 30_000,
    revalidateOnFocus: false,
  });

  const pivot = useMemo<Pivot | null>(
    () => (data ? pivotCells(data.cells) : null),
    [data],
  );

  const [isBackfilling, setIsBackfilling] = useState(false);
  const [backfillError, setBackfillError] = useState<string | null>(null);

  const onBackfill = async () => {
    setBackfillError(null);
    setIsBackfilling(true);
    try {
      const res = await fetch(
        `/api/experiments/${encodedId}/backfill`,
        { method: "POST", credentials: "include" },
      );
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || `Backfill failed (${res.status})`);
      }
      await mutate();
    } catch (err) {
      setBackfillError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsBackfilling(false);
    }
  };

  const onUpdateTarget = async (cellId: string, target: number) => {
    const res = await fetch(
      `/api/experiments/${encodedId}/cells/${encodeURIComponent(cellId)}`,
      {
        method: "PATCH",
        credentials: "include",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ target_n_trials: target }),
      },
    );
    if (!res.ok) {
      const text = await res.text();
      throw new Error(text || `Update failed (${res.status})`);
    }
    await mutate();
  };

  const onDeleteCell = async (cellId: string) => {
    const res = await fetch(
      `/api/experiments/${encodedId}/cells/${encodeURIComponent(cellId)}`,
      { method: "DELETE", credentials: "include" },
    );
    if (!res.ok) {
      const text = await res.text();
      throw new Error(text || `Delete failed (${res.status})`);
    }
    await mutate();
  };

  if (isLoading) {
    return (
      <div className="space-y-2">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-32 w-full" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm">
        Failed to load cell matrix: {String((error as Error).message)}
      </div>
    );
  }

  if (!data || !pivot || pivot.rows.length === 0) {
    return (
      <div className="space-y-3">
        <div className="rounded-md border border-dashed p-6 text-center text-sm text-muted-foreground">
          No cells yet. Add task versions and agents to start collecting
          evidence.
        </div>
        {canEdit ? (
          <div className="flex justify-end">
            <AddCellDialog
              experimentId={experimentId}
              onCreated={async () => {
                await mutate();
              }}
            />
          </div>
        ) : null}
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-3 text-sm">
          <span className="font-medium">{data.experiment_name}</span>
          <Badge variant={data.total_gap === 0 ? "secondary" : "outline"}>
            gap: {data.total_gap}
          </Badge>
          <span className="text-muted-foreground">
            {pivot.rows.length} task version{pivot.rows.length === 1 ? "" : "s"} ·{" "}
            {pivot.agents.length} agent{pivot.agents.length === 1 ? "" : "s"}
          </span>
        </div>
        {canEdit ? (
          <div className="flex items-center gap-2">
            <AddCellDialog
              experimentId={experimentId}
              onCreated={async () => {
                await mutate();
              }}
            />
            <Button
              size="sm"
              onClick={onBackfill}
              disabled={isBackfilling || data.total_gap === 0}
              variant={data.total_gap > 0 ? "default" : "outline"}
            >
              {isBackfilling
                ? "Enqueuing…"
                : data.total_gap === 0
                  ? "No gaps"
                  : `Backfill ${data.total_gap}`}
            </Button>
          </div>
        ) : null}
      </div>

      {backfillError ? (
        <div className="rounded-md border border-destructive/40 bg-destructive/5 p-2 text-xs">
          {backfillError}
        </div>
      ) : null}

      <div className="overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="sticky left-0 z-10 bg-background">
                Task version
              </TableHead>
              {pivot.agents.map(({ key, agent }) => (
                <TableHead
                  key={key}
                  className="whitespace-nowrap text-center font-mono text-xs"
                >
                  {formatAgentLabel(agent)}
                  <div className="text-[10px] font-normal text-muted-foreground">
                    {agent.provider}
                  </div>
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {pivot.rows.map((row) => (
              <TableRow key={row.taskVersionId}>
                <TableCell className="sticky left-0 z-10 bg-background">
                  <div className="flex flex-col">
                    <span className="font-medium">
                      {row.taskName ?? row.taskId}
                    </span>
                    <span className="text-xs text-muted-foreground">
                      v{row.version ?? "?"}
                    </span>
                  </div>
                </TableCell>
                {pivot.agents.map(({ key }) => (
                  <TableCell key={key} className="text-center">
                    <CellBadge
                      cell={row.cellsByAgent[key]}
                      canEdit={canEdit}
                      experimentId={experimentId}
                      onUpdateTarget={onUpdateTarget}
                      onDelete={onDeleteCell}
                    />
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}

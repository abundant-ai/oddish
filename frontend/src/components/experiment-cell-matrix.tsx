"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
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
import { Checkbox } from "@/components/ui/checkbox";
import { TrialInspectDrawer } from "@/components/trial-inspect-drawer";
import { ExperimentPassAtKGraph } from "@/components/experiment-pass-at-k";
import { ExperimentLeaderboard } from "@/components/experiment-leaderboard";
import { Plus, Pencil, X, Search, Eye, ExternalLink } from "lucide-react";
import { fetcher } from "@/lib/api";
import type {
  ExperimentCellAgent,
  ExperimentTaskRef,
  JobListResponse,
  JobSummary,
  ResolvedExperiment,
  ResolvedExperimentCell,
} from "@/lib/types";
import { encodeExperimentRouteParam } from "@/lib/utils";

type Mode = "view" | "edit";

type Props = {
  experimentId: string;
  canEdit: boolean;
};

interface BrowseTask {
  id: string;
  name: string;
  current_version: number | null;
  current_version_id: string | null;
  tags?: Record<string, string>;
}

interface KnownAgent {
  harness: string;
  model: string | null;
  provider: string;
  equivalence_key: string;
  trial_count: number;
  last_seen: string | null;
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

function modelSubtitle(agent: ExperimentCellAgent): string {
  const harnessLower = agent.harness.toLowerCase();
  if (harnessLower === "oracle") return "reference solution";
  if (harnessLower === "nop") return "do-nothing baseline";
  if (agent.model && agent.model !== "default") return agent.model;
  return agent.provider && agent.provider !== "default" ? agent.provider : "";
}

function shouldShowProvider(provider: string | undefined | null): boolean {
  return Boolean(provider) && provider !== "default";
}

function formatReward(reward: number | null | undefined): string {
  if (reward === null || reward === undefined) return "—";
  return reward.toFixed(2);
}

// ---------------------------------------------------------------------------
// Recent jobs strip
// ---------------------------------------------------------------------------

function jobLabel(job: JobSummary): string {
  if (job.name) return job.name;
  if (job.kind === "experiment_backfill") return "Backfill";
  if (job.kind === "validation") return "Validation";
  if (job.kind === "ad_hoc") return "Ad-hoc";
  return job.kind;
}

function jobIsActive(job: JobSummary): boolean {
  return job.running_trial_count > 0 || job.finished_at === null;
}

function RecentJobs({
  experimentId,
  refreshKey,
}: {
  experimentId: string;
  refreshKey: number;
}) {
  const encodedId = encodeExperimentRouteParam(experimentId);
  const url = `/api/jobs?triggered_by_experiment_id=${encodedId}&limit=3`;
  const { data } = useSWR<JobListResponse>(
    [url, refreshKey],
    ([u]) => fetcher(u as string),
    { refreshInterval: 15_000, revalidateOnFocus: false },
  );

  const jobs = data?.items ?? [];
  if (jobs.length === 0) return null;

  return (
    <div className="flex flex-wrap items-center gap-1.5 text-xs">
      <span className="text-muted-foreground">recent:</span>
      {jobs.map((job) => {
        const done = job.succeeded_trial_count + job.failed_trial_count;
        const active = jobIsActive(job);
        return (
          <Link
            key={job.id}
            href={`/jobs/${encodeURIComponent(job.id)}`}
            className={`inline-flex items-center gap-1 rounded-sm border px-1.5 py-0.5 font-mono hover:bg-muted/50 ${
              active
                ? "border-amber-500/40 bg-amber-500/10"
                : "border-border"
            }`}
            title={
              active
                ? `${job.running_trial_count} running · ${done}/${job.trial_count} done`
                : `${done}/${job.trial_count} done`
            }
          >
            <span>{jobLabel(job)}</span>
            <span className="text-muted-foreground">
              {done}/{job.trial_count}
            </span>
            <ExternalLink className="h-2.5 w-2.5 text-muted-foreground" />
          </Link>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Trials-per-cell input
// ---------------------------------------------------------------------------

function DefaultTargetEditor({
  value,
  canEdit,
  onApply,
}: {
  value: number;
  canEdit: boolean;
  onApply: (next: number) => Promise<void>;
}) {
  const [draft, setDraft] = useState(String(value));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDraft(String(value));
  }, [value]);

  const dirty =
    Math.max(1, parseInt(draft, 10) || 1) !== value && draft.trim() !== "";

  if (!canEdit) {
    return (
      <span className="inline-flex items-center gap-1.5 text-sm">
        <span className="text-muted-foreground">Trials each:</span>
        <span className="font-mono">{value}</span>
      </span>
    );
  }

  return (
    <div className="inline-flex flex-wrap items-center gap-1.5">
      <Label
        htmlFor="trials-each"
        className="whitespace-nowrap text-sm text-muted-foreground"
      >
        Trials each:
      </Label>
      <Input
        id="trials-each"
        type="number"
        min={1}
        className="h-7 w-16 text-xs"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
      />
      {dirty ? (
        <Button
          size="sm"
          className="h-7"
          disabled={busy}
          onClick={async () => {
            const n = Math.max(1, parseInt(draft, 10) || 1);
            setBusy(true);
            setError(null);
            try {
              await onApply(n);
            } catch (err) {
              setError(err instanceof Error ? err.message : String(err));
            } finally {
              setBusy(false);
            }
          }}
        >
          {busy ? "…" : "Apply"}
        </Button>
      ) : null}
      {error ? (
        <span className="text-[11px] text-destructive">{error}</span>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Per-intersection trials list dialog
// ---------------------------------------------------------------------------

function TrialsListDialog({
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
          <div className="rounded-sm border border-destructive/40 bg-destructive/5 p-2 text-xs">
            {String((error as Error).message)}
          </div>
        ) : isLoading || !data ? (
          <Skeleton className="h-40" />
        ) : data.length === 0 ? (
          <div className="rounded-sm border border-dashed p-6 text-center text-sm text-muted-foreground">
            No trials yet.
          </div>
        ) : (
          <div className="max-h-[60vh] overflow-y-auto rounded-sm border">
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

// ---------------------------------------------------------------------------
// Intersection cell
// ---------------------------------------------------------------------------

function IntersectionCell({
  cell,
  editable,
  experimentId,
  onUpdateTarget,
}: {
  cell: ResolvedExperimentCell;
  editable: boolean;
  experimentId: string;
  onUpdateTarget: (cellId: string, target: number) => Promise<void>;
}) {
  const [editingTarget, setEditingTarget] = useState(false);
  const [draft, setDraft] = useState<string>(String(cell.target_n_trials));
  const [trialsOpen, setTrialsOpen] = useState(false);

  const ratioColor =
    cell.gap === 0
      ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300"
      : cell.have_n_running > 0
        ? "bg-amber-500/15 text-amber-700 dark:text-amber-300"
        : "bg-rose-500/15 text-rose-700 dark:text-rose-300";

  const badge = (
    <div
      className={`inline-flex flex-col items-start gap-0.5 rounded-sm px-2 py-1 ${ratioColor}`}
    >
      <span className="font-mono text-xs">
        {cell.have_n_successful}/{cell.target_n_trials}
      </span>
      <span className="text-[10px] opacity-80">
        avg {formatReward(cell.mean_reward)}
      </span>
    </div>
  );

  return (
    <>
      <Popover>
        <PopoverTrigger asChild>
          <button type="button" className="inline-flex" aria-label="Inspect">
            {badge}
          </button>
        </PopoverTrigger>
        <PopoverContent className="w-72 space-y-2 p-3 text-xs">
          <div className="font-mono">
            successful: {cell.have_n_successful} · failed: {cell.have_n_failed}{" "}
            · running: {cell.have_n_running}
          </div>
          <div>
            target: {cell.target_n_trials} ·{" "}
            <span className="font-semibold">{cell.gap}</span> still to run
          </div>
          {cell.last_run_at ? (
            <div className="text-muted-foreground">
              last run {new Date(cell.last_run_at).toLocaleString()}
            </div>
          ) : null}
          {editable && editingTarget ? (
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
                  setEditingTarget(false);
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
                  setEditingTarget(false);
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
              {editable ? (
                <Button
                  size="sm"
                  variant="outline"
                  className="h-7"
                  onClick={() => setEditingTarget(true)}
                >
                  <Pencil className="mr-1 h-3 w-3" /> Override target
                </Button>
              ) : null}
            </div>
          )}
        </PopoverContent>
      </Popover>
      <TrialsListDialog
        experimentId={experimentId}
        cellId={cell.id}
        open={trialsOpen}
        onOpenChange={setTrialsOpen}
        agent={cell.agent}
      />
    </>
  );
}

// ---------------------------------------------------------------------------
// Pickers
// ---------------------------------------------------------------------------

function PickAgentsDialog({
  open,
  onOpenChange,
  excludeKeys,
  onAdd,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  excludeKeys: Set<string>;
  onAdd: (agents: KnownAgent[]) => Promise<void>;
}) {
  const { data } = useSWR<KnownAgent[]>(
    open ? "/api/agents/known?limit=200" : null,
    fetcher,
  );
  const [query, setQuery] = useState("");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const all = (data ?? []).filter((a) => !excludeKeys.has(a.equivalence_key));
    if (!q) return all;
    return all.filter(
      (a) =>
        a.harness.toLowerCase().includes(q) ||
        (a.model ?? "").toLowerCase().includes(q) ||
        a.provider.toLowerCase().includes(q),
    );
  }, [data, query, excludeKeys]);

  const reset = () => {
    setQuery("");
    setPicked(new Set());
    setError(null);
  };

  const submit = async () => {
    if (picked.size === 0) {
      onOpenChange(false);
      return;
    }
    setError(null);
    setBusy(true);
    try {
      const chosen = (data ?? []).filter((a) => picked.has(a.equivalence_key));
      await onAdd(chosen);
      reset();
      onOpenChange(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        onOpenChange(o);
        if (!o) reset();
      }}
    >
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Add agent</DialogTitle>
        </DialogHeader>
        <div className="space-y-2 text-sm">
          <div className="flex items-center gap-2">
            <Search className="h-4 w-4 text-muted-foreground" />
            <Input
              placeholder="Filter by harness, model, provider"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              className="h-8"
            />
            <span className="whitespace-nowrap text-xs text-muted-foreground">
              {picked.size} selected
            </span>
          </div>
          {!data ? (
            <Skeleton className="h-48" />
          ) : filtered.length === 0 ? (
            <div className="rounded-sm border border-dashed p-6 text-center text-xs text-muted-foreground">
              {(data ?? []).length === 0
                ? "No agents have run yet."
                : "All known agents are already on this experiment."}
            </div>
          ) : (
            <div className="max-h-[50vh] overflow-y-auto rounded-sm border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-10" />
                    <TableHead>Agent</TableHead>
                    <TableHead className="text-right">Seen</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filtered.map((a) => (
                    <TableRow
                      key={a.equivalence_key}
                      className="cursor-pointer"
                      onClick={() => {
                        const next = new Set(picked);
                        if (next.has(a.equivalence_key))
                          next.delete(a.equivalence_key);
                        else next.add(a.equivalence_key);
                        setPicked(next);
                      }}
                    >
                      <TableCell>
                        <Checkbox
                          checked={picked.has(a.equivalence_key)}
                          onCheckedChange={() => {}}
                        />
                      </TableCell>
                      <TableCell className="font-mono text-xs">
                        {a.harness}
                        {a.model && a.model !== "default"
                          ? ` · ${a.model}`
                          : ""}
                        {shouldShowProvider(a.provider) ? (
                          <span className="ml-1 text-[10px] text-muted-foreground">
                            ({a.provider})
                          </span>
                        ) : null}
                      </TableCell>
                      <TableCell className="text-right font-mono text-xs">
                        {a.trial_count}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
          {error ? (
            <div className="rounded-sm border border-destructive/40 bg-destructive/5 p-2 text-xs">
              {error}
            </div>
          ) : null}
        </div>
        <DialogFooter>
          <Button
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={busy}
          >
            Cancel
          </Button>
          <Button onClick={submit} disabled={busy || picked.size === 0}>
            {busy
              ? "Adding…"
              : `Add ${picked.size} agent${picked.size === 1 ? "" : "s"}`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function PickTasksDialog({
  open,
  onOpenChange,
  excludeIds,
  onAdd,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  excludeIds: Set<string>;
  onAdd: (tasks: BrowseTask[]) => Promise<void>;
}) {
  const { data } = useSWR<{ items: BrowseTask[] }>(
    open ? "/api/tasks/browse?limit=500&offset=0" : null,
    fetcher,
  );
  const [query, setQuery] = useState("");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const tasks = useMemo(
    () =>
      (data?.items ?? []).filter(
        (t) =>
          t.current_version_id !== null && !excludeIds.has(t.current_version_id),
      ),
    [data, excludeIds],
  );

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return tasks;
    return tasks.filter(
      (t) =>
        t.name.toLowerCase().includes(q) ||
        Object.entries(t.tags ?? {}).some(
          ([k, v]) =>
            k.toLowerCase().includes(q) || v.toLowerCase().includes(q),
        ),
    );
  }, [tasks, query]);

  const reset = () => {
    setQuery("");
    setPicked(new Set());
    setError(null);
  };

  const submit = async () => {
    if (picked.size === 0) {
      onOpenChange(false);
      return;
    }
    setError(null);
    setBusy(true);
    try {
      const chosen = tasks.filter((t) => picked.has(t.id));
      await onAdd(chosen);
      reset();
      onOpenChange(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        onOpenChange(o);
        if (!o) reset();
      }}
    >
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Add task</DialogTitle>
        </DialogHeader>
        <div className="space-y-2 text-sm">
          <div className="flex items-center gap-2">
            <Search className="h-4 w-4 text-muted-foreground" />
            <Input
              placeholder="Filter by task name or tag"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              className="h-8"
            />
            <span className="whitespace-nowrap text-xs text-muted-foreground">
              {picked.size} selected
            </span>
          </div>
          {!data ? (
            <Skeleton className="h-48" />
          ) : filtered.length === 0 ? (
            <div className="rounded-sm border border-dashed p-6 text-center text-xs text-muted-foreground">
              {tasks.length === 0
                ? "All known tasks are already on this experiment."
                : "No tasks match."}
            </div>
          ) : (
            <div className="max-h-[50vh] overflow-y-auto rounded-sm border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-10" />
                    <TableHead>Task</TableHead>
                    <TableHead>Version</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filtered.map((t) => (
                    <TableRow
                      key={t.id}
                      className="cursor-pointer"
                      onClick={() => {
                        const next = new Set(picked);
                        if (next.has(t.id)) next.delete(t.id);
                        else next.add(t.id);
                        setPicked(next);
                      }}
                    >
                      <TableCell>
                        <Checkbox
                          checked={picked.has(t.id)}
                          onCheckedChange={() => {}}
                        />
                      </TableCell>
                      <TableCell className="font-mono text-xs">
                        {t.name}
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        v{t.current_version ?? "?"}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
          {error ? (
            <div className="rounded-sm border border-destructive/40 bg-destructive/5 p-2 text-xs">
              {error}
            </div>
          ) : null}
        </div>
        <DialogFooter>
          <Button
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={busy}
          >
            Cancel
          </Button>
          <Button onClick={submit} disabled={busy || picked.size === 0}>
            {busy
              ? "Adding…"
              : `Add ${picked.size} task${picked.size === 1 ? "" : "s"}`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Main matrix
// ---------------------------------------------------------------------------

function cellKey(taskVersionId: string, agentEqKey: string): string {
  return `${taskVersionId}::${agentEqKey}`;
}

export function ExperimentCellMatrix({ experimentId, canEdit }: Props) {
  const encodedId = encodeExperimentRouteParam(experimentId);
  const url = experimentId ? `/api/experiments/${encodedId}/resolved` : null;

  const { data, error, isLoading, mutate } = useSWR<ResolvedExperiment>(
    url,
    fetcher,
    {
      refreshInterval: 60_000,
      revalidateOnFocus: false,
    },
  );

  const [mode, setMode] = useState<Mode>("view");
  const [pickAgentsOpen, setPickAgentsOpen] = useState(false);
  const [pickTasksOpen, setPickTasksOpen] = useState(false);
  const [busyMsg, setBusyMsg] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [isBackfilling, setIsBackfilling] = useState(false);
  const [jobsRefreshKey, setJobsRefreshKey] = useState(0);
  const [hiddenAgents, setHiddenAgents] = useState<Set<string>>(new Set());

  const toggleHiddenAgent = (eqKey: string) => {
    setHiddenAgents((prev) => {
      const next = new Set(prev);
      if (next.has(eqKey)) next.delete(eqKey);
      else next.add(eqKey);
      return next;
    });
  };

  const targetN = data?.target_n_trials ?? 3;
  const tasks: ExperimentTaskRef[] = data?.tasks ?? [];
  const agents: ExperimentCellAgent[] = data?.agents ?? [];

  // Index intersections by (task_version, agent_eq_key) for O(1) lookup.
  const cellsByKey = useMemo(() => {
    const m = new Map<string, ResolvedExperimentCell>();
    for (const c of data?.cells ?? []) {
      m.set(cellKey(c.task_version_id, c.agent.equivalence_key), c);
    }
    return m;
  }, [data]);

  const editable = canEdit && mode === "edit";

  const callBulk = async (body: Record<string, unknown>) => {
    const res = await fetch(`/api/experiments/${encodedId}/cells/bulk`, {
      method: "POST",
      credentials: "include",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(text || `Failed (${res.status})`);
    }
  };

  const onBackfill = async () => {
    setActionError(null);
    setIsBackfilling(true);
    try {
      const res = await fetch(`/api/experiments/${encodedId}/backfill`, {
        method: "POST",
        credentials: "include",
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || `Backfill failed (${res.status})`);
      }
      setJobsRefreshKey((k) => k + 1);
      await mutate();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
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

  const onApplyDefaultTarget = async (next: number) => {
    setActionError(null);
    await callBulk({ op: "set_default_target", target_n_trials: next });
    await mutate();
  };

  const onAddAgents = async (chosen: KnownAgent[]) => {
    setActionError(null);
    setBusyMsg("Adding agent…");
    try {
      for (const a of chosen) {
        await callBulk({
          op: "add_agent",
          target_n_trials: targetN,
          agent_harness: a.harness,
          agent_model: a.model,
          agent_provider: a.provider,
        });
      }
      await mutate();
    } finally {
      setBusyMsg(null);
    }
  };

  const onAddTasks = async (chosen: BrowseTask[]) => {
    setActionError(null);
    setBusyMsg("Adding task…");
    try {
      for (const t of chosen) {
        if (!t.current_version_id) continue;
        await callBulk({
          op: "add_task",
          target_n_trials: targetN,
          task_version_id: t.current_version_id,
        });
      }
      await mutate();
    } finally {
      setBusyMsg(null);
    }
  };

  const onDeleteAgent = async (agent: ExperimentCellAgent) => {
    if (
      !window.confirm(
        `Remove ${agent.harness}${agent.model ? ` · ${agent.model}` : ""} from this experiment? Trial history is kept.`,
      )
    )
      return;
    setActionError(null);
    setBusyMsg("Removing agent…");
    try {
      await callBulk({
        op: "delete_agent",
        target_n_trials: targetN,
        agent_harness: agent.harness,
        agent_model: agent.model,
        agent_provider: agent.provider,
      });
      await mutate();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyMsg(null);
    }
  };

  const onDeleteTask = async (task: ExperimentTaskRef) => {
    const label = `${task.task_name ?? task.task_id} v${task.task_version ?? "?"}`;
    if (
      !window.confirm(
        `Remove ${label} from this experiment? Trial history is kept.`,
      )
    )
      return;
    setActionError(null);
    setBusyMsg("Removing task…");
    try {
      await callBulk({
        op: "delete_task",
        target_n_trials: targetN,
        task_version_id: task.task_version_id,
      });
      await mutate();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyMsg(null);
    }
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
      <div className="rounded-sm border border-destructive/40 bg-destructive/5 p-3 text-sm">
        Failed to load matrix: {String((error as Error).message)}
      </div>
    );
  }

  if (!data) return null;

  const existingTaskVersionIds = new Set(tasks.map((t) => t.task_version_id));
  const existingAgentKeys = new Set(agents.map((a) => a.equivalence_key));

  return (
    <div className="space-y-3">
      {/* Toolbar */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-3 text-sm">
          <DefaultTargetEditor
            value={targetN}
            canEdit={canEdit}
            onApply={onApplyDefaultTarget}
          />
          <span className="text-xs text-muted-foreground">
            {tasks.length} task{tasks.length === 1 ? "" : "s"} ·{" "}
            {agents.length} agent{agents.length === 1 ? "" : "s"}
          </span>
          <RecentJobs
            experimentId={experimentId}
            refreshKey={jobsRefreshKey}
          />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {canEdit ? (
            <div className="inline-flex items-center overflow-hidden rounded-sm border">
              <button
                type="button"
                onClick={() => setMode("view")}
                className={`flex items-center gap-1 px-2.5 py-1 text-xs ${
                  mode === "view"
                    ? "bg-foreground text-background"
                    : "text-muted-foreground hover:text-foreground"
                }`}
              >
                <Eye className="h-3 w-3" /> View
              </button>
              <button
                type="button"
                onClick={() => setMode("edit")}
                className={`flex items-center gap-1 border-l px-2.5 py-1 text-xs ${
                  mode === "edit"
                    ? "bg-foreground text-background"
                    : "text-muted-foreground hover:text-foreground"
                }`}
              >
                <Pencil className="h-3 w-3" /> Edit
              </button>
            </div>
          ) : null}
          {canEdit ? (
            <Button
              size="sm"
              onClick={onBackfill}
              disabled={isBackfilling || data.total_gap === 0}
              variant={data.total_gap > 0 ? "default" : "outline"}
            >
              {isBackfilling
                ? "Enqueuing…"
                : data.total_gap === 0
                  ? "Up to date"
                  : `Run ${data.total_gap} trial${data.total_gap === 1 ? "" : "s"}`}
            </Button>
          ) : null}
        </div>
      </div>

      {actionError ? (
        <div className="rounded-sm border border-destructive/40 bg-destructive/5 p-2 text-xs">
          {actionError}
        </div>
      ) : null}
      {busyMsg ? (
        <div className="rounded-sm border bg-muted/30 p-2 text-xs text-muted-foreground">
          {busyMsg}
        </div>
      ) : null}

      {/* Graph + leaderboard sit above the matrix when there's enough
          evidence to compute pass@k. Both auto-hide if the data is
          too sparse. */}
      <ExperimentPassAtKGraph
        data={data}
        hiddenAgents={hiddenAgents}
        onToggleAgent={toggleHiddenAgent}
      />
      <ExperimentLeaderboard data={data} />

      {/* Empty + view mode: nothing to show */}
      {!editable && tasks.length === 0 && agents.length === 0 ? (
        <div className="rounded-sm border border-dashed p-6 text-center text-sm text-muted-foreground">
          Empty experiment. Switch to Edit to add tasks and agents.
        </div>
      ) : (
        <div className="max-h-[70vh] overflow-auto rounded-sm border bg-card">
          <Table>
            <TableHeader className="sticky top-0 z-20">
              <TableRow className="border-b-2 border-border bg-muted hover:bg-muted">
                {/* Top-left corner: row #s + Task header */}
                <TableHead className="sticky left-0 z-30 w-48 border-r border-border bg-muted font-mono font-bold text-foreground">
                  <div className="flex items-center gap-2">
                    <span className="w-6 flex-shrink-0 text-right text-xs text-muted-foreground">
                      #
                    </span>
                    <span className="text-xs">Task</span>
                  </div>
                </TableHead>
                {agents.map((agent) => (
                  <TableHead
                    key={agent.equivalence_key}
                    className="border-r border-border bg-muted px-2 text-center font-mono last:border-r-0"
                  >
                    <div className="flex flex-col items-center gap-0.5">
                      <div className="inline-flex items-center gap-1 text-xs font-bold text-foreground">
                        <span className="truncate">{agent.harness}</span>
                        {editable ? (
                          <button
                            type="button"
                            aria-label="Remove agent"
                            title="Remove agent"
                            className="rounded-sm p-0.5 text-muted-foreground hover:bg-destructive/15 hover:text-destructive"
                            onClick={() => onDeleteAgent(agent)}
                          >
                            <X className="h-3 w-3" />
                          </button>
                        ) : null}
                      </div>
                      <div className="truncate text-[10px] font-normal text-muted-foreground">
                        {modelSubtitle(agent)}
                      </div>
                    </div>
                  </TableHead>
                ))}
                {editable ? (
                  <TableHead className="w-12 bg-muted text-center">
                    <button
                      type="button"
                      aria-label="Add agent"
                      title="Add agent"
                      className="inline-flex h-7 w-7 items-center justify-center rounded-sm border border-dashed text-muted-foreground hover:border-foreground hover:text-foreground"
                      onClick={() => setPickAgentsOpen(true)}
                    >
                      <Plus className="h-3.5 w-3.5" />
                    </button>
                  </TableHead>
                ) : null}
              </TableRow>
            </TableHeader>
            <TableBody>
              {tasks.map((task, idx) => (
                <TableRow
                  key={task.task_version_id}
                  className={
                    idx % 2 === 0
                      ? "bg-background hover:bg-muted/30"
                      : "bg-muted/20 hover:bg-muted/40"
                  }
                >
                  <TableCell className="sticky left-0 z-10 w-48 border-r border-border bg-card px-2 font-mono text-xs">
                    <div className="flex items-center gap-2">
                      <span className="w-6 flex-shrink-0 text-right text-muted-foreground">
                        {idx + 1}
                      </span>
                      <div className="flex min-w-0 flex-col">
                        <span className="truncate font-bold" title={task.task_name ?? task.task_id}>
                          {task.task_name ?? task.task_id}
                        </span>
                        <span className="text-[10px] font-normal text-muted-foreground">
                          v{task.task_version ?? "?"}
                        </span>
                      </div>
                      {editable ? (
                        <button
                          type="button"
                          aria-label="Remove task"
                          title="Remove task"
                          className="ml-auto rounded-sm p-0.5 text-muted-foreground hover:bg-destructive/15 hover:text-destructive"
                          onClick={() => onDeleteTask(task)}
                        >
                          <X className="h-3.5 w-3.5" />
                        </button>
                      ) : null}
                    </div>
                  </TableCell>
                  {agents.map((agent) => {
                    const cell = cellsByKey.get(
                      cellKey(task.task_version_id, agent.equivalence_key),
                    );
                    return (
                      <TableCell
                        key={agent.equivalence_key}
                        className="border-r border-border p-1.5 text-center last:border-r-0"
                      >
                        {cell ? (
                          <IntersectionCell
                            cell={cell}
                            editable={editable}
                            experimentId={experimentId}
                            onUpdateTarget={onUpdateTarget}
                          />
                        ) : (
                          <span className="text-xs text-muted-foreground">
                            —
                          </span>
                        )}
                      </TableCell>
                    );
                  })}
                  {editable ? <TableCell /> : null}
                </TableRow>
              ))}
              {editable ? (
                <TableRow>
                  <TableCell className="sticky left-0 z-10 w-48 border-r border-border bg-card px-2">
                    <button
                      type="button"
                      aria-label="Add task"
                      title="Add task"
                      className="inline-flex h-7 items-center gap-1 rounded-sm border border-dashed px-2 text-xs text-muted-foreground hover:border-foreground hover:text-foreground"
                      onClick={() => setPickTasksOpen(true)}
                    >
                      <Plus className="h-3.5 w-3.5" /> task
                    </button>
                  </TableCell>
                  {agents.map((agent) => (
                    <TableCell
                      key={`new-${agent.equivalence_key}`}
                      className="border-r border-border last:border-r-0"
                    />
                  ))}
                  <TableCell />
                </TableRow>
              ) : null}
            </TableBody>
          </Table>
        </div>
      )}

      <PickAgentsDialog
        open={pickAgentsOpen}
        onOpenChange={setPickAgentsOpen}
        excludeKeys={existingAgentKeys}
        onAdd={onAddAgents}
      />
      <PickTasksDialog
        open={pickTasksOpen}
        onOpenChange={setPickTasksOpen}
        excludeIds={existingTaskVersionIds}
        onAdd={onAddTasks}
      />
    </div>
  );
}

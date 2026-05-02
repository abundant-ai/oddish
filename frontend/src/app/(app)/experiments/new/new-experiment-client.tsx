"use client";

import { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import useSWR from "swr";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Checkbox } from "@/components/ui/checkbox";
import { fetcher } from "@/lib/api";
import { encodeExperimentRouteParam } from "@/lib/utils";
import {
  ChevronLeft,
  ChevronRight,
  Search,
  Check,
  X as XIcon,
} from "lucide-react";
import type {
  ResolvedExperiment,
  ResolvedExperimentCell,
} from "@/lib/types";

type Step = "name" | "tasks" | "agents" | "preview";

interface BrowseTask {
  id: string;
  name: string;
  current_version: number | null;
  current_version_id: string | null;
  tags?: Record<string, string>;
}

interface BrowseResponse {
  items: BrowseTask[];
}

interface KnownAgent {
  harness: string;
  model: string | null;
  provider: string;
  equivalence_key: string;
  trial_count: number;
  last_seen: string | null;
}

interface CellPayload {
  task_version_id: string;
  agent_harness: string;
  agent_model: string | null;
  agent_provider: string;
  target_n_trials: number;
}

const STEPS: { value: Step; label: string }[] = [
  { value: "name", label: "Name" },
  { value: "tasks", label: "Task versions" },
  { value: "agents", label: "Agents" },
  { value: "preview", label: "Preview" },
];

function stepIndex(step: Step): number {
  return STEPS.findIndex((s) => s.value === step);
}

function fmtAgentLabel(a: KnownAgent): string {
  const model = a.model && a.model !== "default" ? ` · ${a.model}` : "";
  const provider =
    a.provider && a.provider !== "default" ? ` (${a.provider})` : "";
  return `${a.harness}${model}${provider}`;
}

function fmtTaskLabel(t: BrowseTask): string {
  return `${t.name} · v${t.current_version ?? "?"}`;
}

export function NewExperimentClient() {
  const router = useRouter();

  const [step, setStep] = useState<Step>("name");
  const [name, setName] = useState("");
  const [taskQuery, setTaskQuery] = useState("");
  const [agentQuery, setAgentQuery] = useState("");
  const [selectedTaskIds, setSelectedTaskIds] = useState<Set<string>>(
    new Set(),
  );
  const [selectedAgentKeys, setSelectedAgentKeys] = useState<Set<string>>(
    new Set(),
  );
  const [targetN, setTargetN] = useState("3");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [preview, setPreview] = useState<ResolvedExperiment | null>(null);

  const { data: browse } = useSWR<BrowseResponse>(
    step !== "name" ? "/api/tasks/browse?limit=500&offset=0" : null,
    fetcher,
  );
  const { data: knownAgents } = useSWR<KnownAgent[]>(
    step === "agents" || step === "preview" ? "/api/agents/known?limit=200" : null,
    fetcher,
  );

  const tasks = useMemo(() => {
    return (browse?.items ?? []).filter((t) => t.current_version_id !== null);
  }, [browse]);

  const filteredTasks = useMemo(() => {
    const q = taskQuery.trim().toLowerCase();
    if (!q) return tasks;
    return tasks.filter(
      (t) =>
        t.name.toLowerCase().includes(q) ||
        Object.entries(t.tags ?? {}).some(
          ([k, v]) =>
            k.toLowerCase().includes(q) || v.toLowerCase().includes(q),
        ),
    );
  }, [tasks, taskQuery]);

  const agents = useMemo(() => knownAgents ?? [], [knownAgents]);
  const filteredAgents = useMemo(() => {
    const q = agentQuery.trim().toLowerCase();
    if (!q) return agents;
    return agents.filter(
      (a) =>
        a.harness.toLowerCase().includes(q) ||
        (a.model ?? "").toLowerCase().includes(q) ||
        a.provider.toLowerCase().includes(q),
    );
  }, [agents, agentQuery]);

  const cellsPayload = useMemo<CellPayload[]>(() => {
    const target = Math.max(1, parseInt(targetN, 10) || 1);
    const out: CellPayload[] = [];
    for (const t of tasks) {
      if (!selectedTaskIds.has(t.id) || !t.current_version_id) continue;
      for (const a of agents) {
        if (!selectedAgentKeys.has(a.equivalence_key)) continue;
        out.push({
          task_version_id: t.current_version_id,
          agent_harness: a.harness,
          agent_model: a.model,
          agent_provider: a.provider,
          target_n_trials: target,
        });
      }
    }
    return out;
  }, [tasks, agents, selectedTaskIds, selectedAgentKeys, targetN]);

  const canAdvance = (() => {
    if (step === "name") return name.trim().length > 0;
    if (step === "tasks") return selectedTaskIds.size > 0;
    if (step === "agents") return selectedAgentKeys.size > 0;
    return cellsPayload.length > 0;
  })();

  const goNext = async () => {
    setError(null);
    const idx = stepIndex(step);
    const nextStep = STEPS[idx + 1]?.value;
    if (!nextStep) return;
    setStep(nextStep);
    if (nextStep === "preview") {
      await loadPreview();
    }
  };

  const goBack = () => {
    setError(null);
    const idx = stepIndex(step);
    const prev = STEPS[idx - 1]?.value;
    if (prev) setStep(prev);
  };

  const loadPreview = async () => {
    setPreviewError(null);
    setPreview(null);
    if (cellsPayload.length === 0) return;
    try {
      // Cheap dry-run: hit the server-side resolve by creating an
      // experiment-shaped object client-side. Backend doesn't have a
      // dedicated preview yet, so we approximate by computing locally
      // -- gaps come from existing trial counts which the matrix would
      // surface anyway. Here we just mirror the cells as
      // ResolvedExperimentCell rows with target = N, have_n = 0 (we
      // don't know yet), so the preview is "what cells will exist".
      const cells: ResolvedExperimentCell[] = cellsPayload.map((c) => ({
        id: `preview-${c.task_version_id}-${c.agent_harness}-${c.agent_model ?? ""}`,
        task_version_id: c.task_version_id,
        task_id:
          tasks.find((t) => t.current_version_id === c.task_version_id)?.id ??
          "",
        task_name:
          tasks.find((t) => t.current_version_id === c.task_version_id)?.name ??
          null,
        task_version:
          tasks.find((t) => t.current_version_id === c.task_version_id)
            ?.current_version ?? null,
        target_n_trials: c.target_n_trials,
        agent: {
          harness: c.agent_harness,
          model: c.agent_model,
          provider: c.agent_provider,
          equivalence_key:
            agents.find(
              (a) =>
                a.harness === c.agent_harness &&
                a.model === c.agent_model &&
                a.provider === c.agent_provider,
            )?.equivalence_key ?? "",
        },
        have_n_total: 0,
        have_n_successful: 0,
        have_n_failed: 0,
        have_n_running: 0,
        gap: c.target_n_trials,
        mean_reward: null,
        last_run_at: null,
      }));
      setPreview({
        experiment_id: "preview",
        experiment_name: name.trim() || "(unnamed)",
        target_n_trials: Math.max(1, parseInt(targetN, 10) || 1),
        tasks: cells.map((c) => ({
          task_version_id: c.task_version_id,
          task_id: c.task_id,
          task_name: c.task_name,
          task_version: c.task_version,
        })),
        agents: cells.map((c) => c.agent),
        cells,
        total_gap: cells.reduce((acc, c) => acc + c.gap, 0),
      });
    } catch (err) {
      setPreviewError(err instanceof Error ? err.message : String(err));
    }
  };

  const onCreate = async (andBackfill: boolean) => {
    setError(null);
    setSubmitting(true);
    try {
      const res = await fetch("/api/experiments", {
        method: "POST",
        credentials: "include",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          name: name.trim(),
          cells: cellsPayload,
        }),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || `Create failed (${res.status})`);
      }
      const created = (await res.json()) as ResolvedExperiment;
      const encodedId = encodeExperimentRouteParam(created.experiment_id);

      if (andBackfill) {
        const bf = await fetch(`/api/experiments/${encodedId}/backfill`, {
          method: "POST",
          credentials: "include",
        });
        if (!bf.ok) {
          const text = await bf.text();
          throw new Error(text || `Backfill failed (${bf.status})`);
        }
      }

      router.push(`/experiments/${encodedId}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  };

  const stepIdx = stepIndex(step);

  return (
    <div className="mx-auto w-full max-w-(--breakpoint-xl) space-y-4 p-4">
      <div>
        <h1 className="text-[24px] font-semibold tracking-[-0.02em]">
          New experiment
        </h1>
        <p className="text-sm text-muted-foreground">
          Pick task versions and agents to evaluate against. Each (task,
          agent) pair becomes a cell with a trial target.
        </p>
      </div>

      {/* Step strip */}
      <div className="flex items-center gap-2 overflow-x-auto rounded-sm border bg-card px-3 py-2 text-xs">
        {STEPS.map((s, i) => {
          const active = i === stepIdx;
          const done = i < stepIdx;
          return (
            <div key={s.value} className="flex items-center gap-2">
              <div
                className={`flex h-5 w-5 items-center justify-center rounded-full border text-[10px] ${
                  active
                    ? "border-foreground bg-foreground text-background"
                    : done
                      ? "border-emerald-500/40 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300"
                      : "border-border bg-muted text-muted-foreground"
                }`}
              >
                {done ? <Check className="h-3 w-3" /> : i + 1}
              </div>
              <span
                className={
                  active
                    ? "font-semibold"
                    : "text-muted-foreground"
                }
              >
                {s.label}
              </span>
              {i < STEPS.length - 1 ? (
                <ChevronRight className="h-3 w-3 text-muted-foreground" />
              ) : null}
            </div>
          );
        })}
      </div>

      <div className="rounded-sm border bg-card p-4">
        {step === "name" ? (
          <div className="space-y-3">
            <div>
              <Label htmlFor="name">Experiment name</Label>
              <Input
                id="name"
                placeholder="e.g. swe-bench-claude-code-2026-05"
                value={name}
                onChange={(e) => setName(e.target.value)}
                autoFocus
              />
              <p className="mt-1 text-xs text-muted-foreground">
                Give it something searchable. You can rename later.
              </p>
            </div>
          </div>
        ) : null}

        {step === "tasks" ? (
          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <Search className="h-4 w-4 text-muted-foreground" />
              <Input
                placeholder="Filter by task name or tag"
                value={taskQuery}
                onChange={(e) => setTaskQuery(e.target.value)}
                className="h-9"
              />
              <span className="whitespace-nowrap text-xs text-muted-foreground">
                {selectedTaskIds.size} selected
              </span>
            </div>
            {!browse ? (
              <Skeleton className="h-64" />
            ) : filteredTasks.length === 0 ? (
              <div className="rounded-sm border border-dashed p-6 text-center text-sm text-muted-foreground">
                No tasks match.
              </div>
            ) : (
              <div className="max-h-[55vh] overflow-y-auto rounded-sm border">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="w-10">
                        <Checkbox
                          checked={
                            filteredTasks.every((t) =>
                              selectedTaskIds.has(t.id),
                            )
                          }
                          onCheckedChange={(v) => {
                            const next = new Set(selectedTaskIds);
                            for (const t of filteredTasks) {
                              if (v) next.add(t.id);
                              else next.delete(t.id);
                            }
                            setSelectedTaskIds(next);
                          }}
                        />
                      </TableHead>
                      <TableHead>Task</TableHead>
                      <TableHead>Version</TableHead>
                      <TableHead>Tags</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {filteredTasks.map((t) => (
                      <TableRow
                        key={t.id}
                        className="cursor-pointer"
                        onClick={() => {
                          const next = new Set(selectedTaskIds);
                          if (next.has(t.id)) next.delete(t.id);
                          else next.add(t.id);
                          setSelectedTaskIds(next);
                        }}
                      >
                        <TableCell>
                          <Checkbox
                            checked={selectedTaskIds.has(t.id)}
                            onCheckedChange={() => {}}
                          />
                        </TableCell>
                        <TableCell className="font-mono text-xs">
                          {t.name}
                        </TableCell>
                        <TableCell className="text-xs text-muted-foreground">
                          v{t.current_version ?? "?"}
                        </TableCell>
                        <TableCell>
                          {t.tags && Object.keys(t.tags).length > 0 ? (
                            <div className="flex flex-wrap gap-1">
                              {Object.entries(t.tags)
                                .filter(([k]) => !k.startsWith("github_"))
                                .slice(0, 3)
                                .map(([k, v]) => (
                                  <Badge
                                    key={k}
                                    variant="secondary"
                                    className="font-mono text-[10px]"
                                  >
                                    {k}={v}
                                  </Badge>
                                ))}
                            </div>
                          ) : null}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}
          </div>
        ) : null}

        {step === "agents" ? (
          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <Search className="h-4 w-4 text-muted-foreground" />
              <Input
                placeholder="Filter by harness, model, provider"
                value={agentQuery}
                onChange={(e) => setAgentQuery(e.target.value)}
                className="h-9"
              />
              <span className="whitespace-nowrap text-xs text-muted-foreground">
                {selectedAgentKeys.size} selected
              </span>
            </div>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-[1fr_180px]">
              <div>
                {!knownAgents ? (
                  <Skeleton className="h-64" />
                ) : filteredAgents.length === 0 ? (
                  <div className="rounded-sm border border-dashed p-6 text-center text-sm text-muted-foreground">
                    No agents have run yet for this org.
                  </div>
                ) : (
                  <div className="max-h-[55vh] overflow-y-auto rounded-sm border">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead className="w-10" />
                          <TableHead>Agent</TableHead>
                          <TableHead className="text-right">Seen in trials</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {filteredAgents.map((a) => (
                          <TableRow
                            key={a.equivalence_key}
                            className="cursor-pointer"
                            onClick={() => {
                              const next = new Set(selectedAgentKeys);
                              if (next.has(a.equivalence_key))
                                next.delete(a.equivalence_key);
                              else next.add(a.equivalence_key);
                              setSelectedAgentKeys(next);
                            }}
                          >
                            <TableCell>
                              <Checkbox
                                checked={selectedAgentKeys.has(
                                  a.equivalence_key,
                                )}
                                onCheckedChange={() => {}}
                              />
                            </TableCell>
                            <TableCell className="font-mono text-xs">
                              {fmtAgentLabel(a)}
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
              </div>
              <div className="rounded-sm border bg-muted/30 p-3 text-sm">
                <Label htmlFor="targetN" className="text-xs uppercase">
                  Target trials per cell
                </Label>
                <Input
                  id="targetN"
                  type="number"
                  min={1}
                  value={targetN}
                  onChange={(e) => setTargetN(e.target.value)}
                  className="mt-1 h-8"
                />
                <p className="mt-2 text-[11px] text-muted-foreground">
                  Each (task, agent) cell will be created with this
                  target. You can adjust per-cell after creation.
                </p>
                <div className="mt-3 border-t pt-3 text-[11px] text-muted-foreground">
                  Will create{" "}
                  <span className="font-mono text-foreground">
                    {selectedTaskIds.size * selectedAgentKeys.size}
                  </span>{" "}
                  cells (
                  <span className="font-mono">{selectedTaskIds.size}</span>{" "}
                  tasks ×{" "}
                  <span className="font-mono">{selectedAgentKeys.size}</span>{" "}
                  agents).
                </div>
              </div>
            </div>
          </div>
        ) : null}

        {step === "preview" ? (
          <div className="space-y-3">
            {previewError ? (
              <div className="rounded-sm border border-destructive/40 bg-destructive/5 p-3 text-sm">
                {previewError}
              </div>
            ) : null}
            {!preview ? (
              <Skeleton className="h-64" />
            ) : (
              <>
                <div className="grid grid-cols-2 gap-3 rounded-sm border bg-muted/30 p-3 text-sm sm:grid-cols-4">
                  <div>
                    <div className="text-[10px] uppercase text-muted-foreground">
                      name
                    </div>
                    <div className="font-mono">{preview.experiment_name}</div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase text-muted-foreground">
                      cells
                    </div>
                    <div className="font-mono">{preview.cells.length}</div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase text-muted-foreground">
                      target trials
                    </div>
                    <div className="font-mono">{preview.total_gap}</div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase text-muted-foreground">
                      tasks × agents
                    </div>
                    <div className="font-mono">
                      {selectedTaskIds.size} × {selectedAgentKeys.size}
                    </div>
                  </div>
                </div>
                <div className="max-h-[40vh] overflow-y-auto rounded-sm border">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Task</TableHead>
                        <TableHead>Agent</TableHead>
                        <TableHead className="text-right">Target</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {preview.cells.map((c) => (
                        <TableRow key={c.id}>
                          <TableCell className="font-mono text-xs">
                            {c.task_name ?? c.task_id}
                            <span className="ml-1 text-[10px] text-muted-foreground">
                              v{c.task_version ?? "?"}
                            </span>
                          </TableCell>
                          <TableCell className="font-mono text-xs">
                            {c.agent.harness}
                            {c.agent.model && c.agent.model !== "default"
                              ? ` · ${c.agent.model}`
                              : ""}
                          </TableCell>
                          <TableCell className="text-right font-mono text-xs">
                            {c.target_n_trials}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              </>
            )}
          </div>
        ) : null}

        {error ? (
          <div className="mt-3 rounded-sm border border-destructive/40 bg-destructive/5 p-2 text-xs">
            {error}
          </div>
        ) : null}
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => router.push("/experiments")}
            disabled={submitting}
          >
            <XIcon className="mr-1 h-3.5 w-3.5" /> Cancel
          </Button>
          {stepIdx > 0 ? (
            <Button
              variant="outline"
              size="sm"
              onClick={goBack}
              disabled={submitting}
            >
              <ChevronLeft className="mr-1 h-3.5 w-3.5" /> Back
            </Button>
          ) : null}
        </div>
        <div className="flex items-center gap-2">
          {step !== "preview" ? (
            <Button
              size="sm"
              onClick={goNext}
              disabled={!canAdvance}
            >
              Next <ChevronRight className="ml-1 h-3.5 w-3.5" />
            </Button>
          ) : (
            <>
              <Button
                size="sm"
                variant="outline"
                onClick={() => onCreate(false)}
                disabled={submitting || cellsPayload.length === 0}
              >
                {submitting ? "Saving…" : "Save experiment"}
              </Button>
              <Button
                size="sm"
                onClick={() => onCreate(true)}
                disabled={submitting || cellsPayload.length === 0}
              >
                {submitting ? "Saving…" : "Save and backfill"}
              </Button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

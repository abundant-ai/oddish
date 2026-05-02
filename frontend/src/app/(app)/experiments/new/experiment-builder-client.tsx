"use client";

import { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import useSWR from "swr";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { fetcher } from "@/lib/api";
import { encodeExperimentRouteParam } from "@/lib/utils";
import type {
  ExperimentCreateResponse,
  TaskBrowseItem,
  TaskBrowseResponse,
  TaskVersion,
} from "@/lib/types";
import { Loader2, Plus, Trash2 } from "lucide-react";

type DraftCell = {
  localId: string;
  taskId: string;
  taskName: string;
  versionId: string;
  version: number;
  harness: string;
  model: string;
  provider: string;
  targetNTrials: number;
};

const defaultAgents = [
  { harness: "codex", model: "gpt-5.2-codex", provider: "openai" },
  { harness: "claude-code", model: "claude-sonnet-4-5", provider: "anthropic" },
  { harness: "gemini-cli", model: "gemini-3-pro-preview", provider: "google" },
];

export function ExperimentBuilderClient() {
  const router = useRouter();
  const [name, setName] = useState("");
  const [selectedTaskId, setSelectedTaskId] = useState("");
  const [selectedVersionId, setSelectedVersionId] = useState("");
  const [harness, setHarness] = useState(defaultAgents[0].harness);
  const [model, setModel] = useState(defaultAgents[0].model);
  const [provider, setProvider] = useState(defaultAgents[0].provider);
  const [targetNTrials, setTargetNTrials] = useState(1);
  const [cells, setCells] = useState<DraftCell[]>([]);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { data: tasksData, error: tasksError } = useSWR<TaskBrowseResponse>(
    "/api/tasks/browse?limit=100&offset=0",
    fetcher,
    { revalidateOnFocus: false }
  );
  const tasks = tasksData?.items ?? [];
  const selectedTask = tasks.find((task) => task.id === selectedTaskId) ?? null;

  const { data: versions = [] } = useSWR<TaskVersion[]>(
    selectedTaskId
      ? `/api/tasks/${encodeURIComponent(selectedTaskId)}/versions`
      : null,
    fetcher,
    { revalidateOnFocus: false }
  );
  const selectedVersion =
    versions.find((version) => version.id === selectedVersionId) ??
    versions[0] ??
    null;

  const totalCells = cells.length;
  const totalTargetTrials = useMemo(
    () => cells.reduce((sum, cell) => sum + cell.targetNTrials, 0),
    [cells]
  );

  function applyAgentPreset(value: string) {
    const preset = defaultAgents.find((agent) => agent.harness === value);
    setHarness(value);
    if (preset) {
      setModel(preset.model);
      setProvider(preset.provider);
    }
  }

  function addCell() {
    if (!selectedTask || !selectedVersion) {
      setError("Pick a task and task version before adding a cell.");
      return;
    }
    if (!harness.trim() || !model.trim()) {
      setError("Harness and model are required.");
      return;
    }
    setError(null);
    setCells((current) => [
      ...current,
      {
        localId: `${selectedVersion.id}:${harness}:${model}:${provider}:${crypto.randomUUID()}`,
        taskId: selectedTask.id,
        taskName: selectedTask.name,
        versionId: selectedVersion.id,
        version: selectedVersion.version,
        harness: harness.trim(),
        model: model.trim(),
        provider: provider.trim(),
        targetNTrials,
      },
    ]);
  }

  async function createExperiment() {
    const experimentName = name.trim();
    if (!experimentName) {
      setError("Experiment name is required.");
      return;
    }
    if (cells.length === 0) {
      setError("Add at least one cell.");
      return;
    }

    setIsSaving(true);
    setError(null);
    try {
      const res = await fetch("/api/experiments", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: experimentName,
          cells: cells.map((cell) => ({
            task_version_id: cell.versionId,
            agent: {
              harness: cell.harness,
              model: cell.model,
              provider: cell.provider || undefined,
            },
            target_n_trials: cell.targetNTrials,
          })),
        }),
      });
      const data = (await res.json().catch(() => null)) as
        | ExperimentCreateResponse
        | { detail?: string; error?: string }
        | null;
      if (!res.ok || !data || !("id" in data)) {
        const errorData = data && !("id" in data) ? data : null;
        throw new Error(
          errorData?.detail || errorData?.error || "Failed to create experiment"
        );
      }
      router.push(`/experiments/${encodeExperimentRouteParam(data.id)}`);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Experiment creation failed"
      );
    } finally {
      setIsSaving(false);
    }
  }

  return (
    <div className="space-y-5">
      <div className="space-y-2">
        <h1 className="font-mono text-2xl font-semibold tracking-tight">
          Build Experiment
        </h1>
        <p className="text-muted-foreground max-w-3xl text-sm">
          Experiments are saved matrices of task versions and agents. Saving a
          cell freezes the task version; backfill later queues a Job for any
          missing evidence.
        </p>
      </div>

      {error || tasksError ? (
        <Alert variant="destructive">
          <AlertTitle>Experiment builder error</AlertTitle>
          <AlertDescription>
            {error || "Failed to load task choices."}
          </AlertDescription>
        </Alert>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-[minmax(0,0.95fr)_minmax(0,1.05fr)]">
        <Card className="border-[#6f88b4]/20 shadow-xs">
          <CardHeader>
            <CardTitle className="text-base">Add Cell</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="experiment-name">Experiment name</Label>
              <Input
                id="experiment-name"
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="terminal-bench canary"
              />
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-2">
                <Label>Task</Label>
                <Select
                  value={selectedTaskId}
                  onValueChange={(value) => {
                    setSelectedTaskId(value);
                    setSelectedVersionId("");
                  }}
                >
                  <SelectTrigger>
                    <SelectValue placeholder="Choose task" />
                  </SelectTrigger>
                  <SelectContent>
                    {tasks.map((task: TaskBrowseItem) => (
                      <SelectItem key={task.id} value={task.id}>
                        {task.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div className="space-y-2">
                <Label>Version</Label>
                <Select
                  value={selectedVersionId || selectedVersion?.id || ""}
                  onValueChange={setSelectedVersionId}
                  disabled={!selectedTaskId || versions.length === 0}
                >
                  <SelectTrigger>
                    <SelectValue placeholder="Choose version" />
                  </SelectTrigger>
                  <SelectContent>
                    {versions.map((version) => (
                      <SelectItem key={version.id} value={version.id}>
                        v{version.version}
                        {version.content_hash
                          ? ` · ${version.content_hash.slice(0, 10)}`
                          : ""}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>

            <div className="grid gap-3 sm:grid-cols-3">
              <div className="space-y-2">
                <Label>Harness</Label>
                <Select value={harness} onValueChange={applyAgentPreset}>
                  <SelectTrigger>
                    <SelectValue placeholder="Harness" />
                  </SelectTrigger>
                  <SelectContent>
                    {defaultAgents.map((agent) => (
                      <SelectItem key={agent.harness} value={agent.harness}>
                        {agent.harness}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-2">
                <Label htmlFor="model">Model</Label>
                <Input
                  id="model"
                  value={model}
                  onChange={(event) => setModel(event.target.value)}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="provider">Provider</Label>
                <Input
                  id="provider"
                  value={provider}
                  onChange={(event) => setProvider(event.target.value)}
                  placeholder="infer"
                />
              </div>
            </div>

            <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
              <div className="space-y-2">
                <Label htmlFor="target">Target trials</Label>
                <Input
                  id="target"
                  type="number"
                  min={0}
                  value={targetNTrials}
                  onChange={(event) =>
                    setTargetNTrials(
                      Math.max(0, Number(event.target.value || 0))
                    )
                  }
                  className="w-36"
                />
              </div>
              <Button type="button" onClick={addCell}>
                <Plus className="mr-2 h-4 w-4" />
                Add cell
              </Button>
            </div>
          </CardContent>
        </Card>

        <Card className="border-[#6f88b4]/20 shadow-xs">
          <CardHeader className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
            <CardTitle className="text-base">Preview Matrix</CardTitle>
            <div className="flex gap-2">
              <Badge variant="outline">{totalCells} cells</Badge>
              <Badge variant="outline">{totalTargetTrials} target trials</Badge>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            {cells.length === 0 ? (
              <div className="bg-card/60 text-muted-foreground rounded-lg border border-dashed border-[#6f88b4]/30 px-6 py-10 text-center text-sm">
                Add task-version and agent pairs to construct the experiment.
              </div>
            ) : (
              <div className="space-y-2">
                {cells.map((cell) => (
                  <div
                    key={cell.localId}
                    className="border-border/70 bg-muted/20 flex flex-col gap-3 rounded-lg border p-3 sm:flex-row sm:items-center sm:justify-between"
                  >
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="truncate font-mono text-sm font-medium">
                          {cell.taskName}
                        </span>
                        <Badge variant="outline">v{cell.version}</Badge>
                      </div>
                      <div className="text-muted-foreground mt-1 truncate text-xs">
                        {cell.harness} · {cell.provider || "inferred"}/
                        {cell.model} · target {cell.targetNTrials}
                      </div>
                    </div>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={() =>
                        setCells((current) =>
                          current.filter(
                            (item) => item.localId !== cell.localId
                          )
                        )
                      }
                    >
                      <Trash2 className="mr-1.5 h-4 w-4" />
                      Remove
                    </Button>
                  </div>
                ))}
              </div>
            )}

            <Button
              type="button"
              className="w-full"
              onClick={createExperiment}
              disabled={isSaving || cells.length === 0}
            >
              {isSaving ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Creating
                </>
              ) : (
                "Create experiment"
              )}
            </Button>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

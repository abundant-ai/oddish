"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

const AGENTS = [
  { value: "claude-code", label: "claude-code" },
  { value: "codex", label: "codex" },
  { value: "gemini-cli", label: "gemini-cli" },
];

const MODELS_BY_AGENT: Record<string, { value: string; label: string }[]> = {
  "claude-code": [
    { value: "anthropic/claude-sonnet-4-6", label: "claude-sonnet-4-6" },
    { value: "anthropic/claude-opus-4-7", label: "claude-opus-4-7" },
  ],
  codex: [{ value: "openai/gpt-5.4-codex", label: "gpt-5.4-codex" }],
  "gemini-cli": [
    { value: "google/gemini-3.1-pro-preview", label: "gemini-3.1-pro-preview" },
  ],
};

// "cheat_ratio" is a legacy alias kept for backward compat when reading old
// stored presets. "ratio" is no longer a valid new metric.
type EvaluationMetric = "result_focus" | "none" | "cheat_ratio";

type Preset = {
  id: string;
  name: string;
  agent: string;
  model: string;
  operator_prompt: string;
  result_focus: string | null;
  evaluation_metric: EvaluationMetric;
  is_seed: boolean;
  created_at: string;
  updated_at: string;
};

function ResultFocusTextarea({
  value,
  onChange,
  rows = 3,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  rows?: number;
  placeholder?: string;
}) {
  const [isSchema, setIsSchema] = useState(false);

  function handleBlur() {
    try {
      const parsed = JSON.parse(value);
      setIsSchema(parsed !== null && typeof parsed === "object");
    } catch {
      setIsSchema(false);
    }
  }

  return (
    <div className="relative mt-1">
      <textarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onBlur={handleBlur}
        rows={rows}
        placeholder={placeholder}
        className="bg-background w-full rounded border px-2 py-1.5 font-mono text-sm"
      />
      {isSchema && (
        <span className="absolute right-2 top-2 rounded bg-blue-500/10 px-1.5 py-0.5 text-[10px] font-medium text-blue-600">
          structured output
        </span>
      )}
    </div>
  );
}

function normalizePreset(p: Preset): Preset {
  // Legacy "cheat_ratio" (and any stale "ratio") fall back to "none".
  if (
    p.evaluation_metric === "cheat_ratio" ||
    (p.evaluation_metric as string) === "ratio"
  ) {
    return { ...p, evaluation_metric: "none" };
  }
  return p;
}

export function ProbeSubmitForm({
  taskId,
  scope = "task",
  onSubmitted,
}: {
  taskId: string;
  scope?: "task" | "experiment";
  experimentId?: string;
  onSubmitted?: () => void;
}) {
  const router = useRouter();
  const [agent, setAgent] = useState("claude-code");
  const [model, setModel] = useState(MODELS_BY_AGENT["claude-code"][0].value);
  const [extraInstructions, setExtraInstructions] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Collapsed by default: a prominent CTA reveals the agent picker + form.
  const [formOpen, setFormOpen] = useState(false);

  const [presets, setPresets] = useState<Preset[]>([]);
  const [presetsLoaded, setPresetsLoaded] = useState(false);
  const [selectedPresetId, setSelectedPresetId] = useState<string>("");
  const [result_focus, setResultFocus] = useState("");

  // Modal state for create/edit preset
  const [modalOpen, setModalOpen] = useState(false);
  const [editingPresetId, setEditingPresetId] = useState<string | null>(null);
  const [modalName, setModalName] = useState("");
  const [modalAgent, setModalAgent] = useState("claude-code");
  const [modalModel, setModalModel] = useState(
    MODELS_BY_AGENT["claude-code"][0].value,
  );
  const [modalOperatorPrompt, setModalOperatorPrompt] = useState("");
  const [modalResultFocus, setModalResultFocus] = useState("");
  const [modalEvaluationMetric, setModalEvaluationMetric] =
    useState<EvaluationMetric>("none");

  const selectedPreset = presets.find((p) => p.id === selectedPresetId) ?? null;

  const reloadPresets = useCallback(async () => {
    try {
      const res = await fetch(`/api/probe-presets`, {
        cache: "no-store",
      });
      if (!res.ok) {
        console.warn("probe presets fetch failed:", res.status);
        return;
      }
      const data = await res.json();
      if (!Array.isArray(data)) return;
      // Backend returns global seeds + this org's custom presets. Normalize
      // legacy cheat_ratio → ratio and backfill a missing metric to "none".
      setPresets(
        (data as Preset[]).map((p) =>
          normalizePreset({
            ...p,
            evaluation_metric: (p.evaluation_metric ??
              "none") as EvaluationMetric,
          }),
        ),
      );
    } catch (err) {
      console.warn("probe presets fetch error:", err);
    } finally {
      setPresetsLoaded(true);
    }
  }, []);

  useEffect(() => {
    void reloadPresets();
  }, [reloadPresets]);

  function loadPreset(id: string) {
    setSelectedPresetId(id);
    if (!id) return;
    const p = presets.find((x) => x.id === id);
    if (!p) return;
    setAgent(p.agent);
    setModel(p.model);
    setExtraInstructions(p.operator_prompt);
    setResultFocus(p.result_focus ?? "");
  }

  function openCreateModal() {
    setEditingPresetId(null);
    setModalName("");
    setModalAgent("claude-code");
    setModalModel(MODELS_BY_AGENT["claude-code"][0].value);
    setModalOperatorPrompt("");
    setModalResultFocus("");
    setModalEvaluationMetric("none");
    setModalOpen(true);
  }

  function openEditModal() {
    if (!selectedPreset) return;
    setEditingPresetId(selectedPreset.id);
    setModalName(selectedPreset.name);
    setModalAgent(selectedPreset.agent);
    setModalModel(selectedPreset.model);
    setModalOperatorPrompt(selectedPreset.operator_prompt);
    setModalResultFocus(selectedPreset.result_focus ?? "");
    setModalEvaluationMetric(selectedPreset.evaluation_metric ?? "none");
    setModalOpen(true);
  }

  async function savePresetFromModal() {
    if (!modalName.trim() || !modalOperatorPrompt.trim()) return;
    const body = {
      name: modalName.trim(),
      agent: modalAgent,
      model: modalModel,
      operator_prompt: modalOperatorPrompt,
      result_focus: modalResultFocus.trim() || null,
      evaluation_metric:
        modalEvaluationMetric === "none" ? null : modalEvaluationMetric,
    };
    // Editing an existing custom preset updates in place (PUT). Creating
    // new — or forking a built-in seed — creates a fresh preset (POST); the
    // backend assigns the id.
    const isEditCustom = editingPresetId !== null && !selectedPreset?.is_seed;
    try {
      const res = await fetch(
        isEditCustom
          ? `/api/probe-presets/${editingPresetId}`
          : `/api/probe-presets`,
        {
          method: isEditCustom ? "PUT" : "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify(body),
        },
      );
      if (!res.ok) {
        setError(`Failed to save preset: HTTP ${res.status}`);
        return;
      }
      const saved = normalizePreset((await res.json()) as Preset);
      await reloadPresets();
      setSelectedPresetId(saved.id);
      // Apply to the current submission form too.
      setAgent(saved.agent);
      setModel(saved.model);
      setExtraInstructions(saved.operator_prompt);
      setResultFocus(saved.result_focus ?? "");
      setModalOpen(false);
    } catch (err) {
      setError(`Failed to save preset: ${String(err)}`);
    }
  }

  async function deleteSelectedPreset() {
    if (!selectedPreset || selectedPreset.is_seed) return;
    if (!confirm(`Delete preset "${selectedPreset.name}"?`)) return;
    try {
      const res = await fetch(`/api/probe-presets/${selectedPreset.id}`, {
        method: "DELETE",
      });
      if (!res.ok) {
        setError(`Failed to delete preset: HTTP ${res.status}`);
        return;
      }
      await reloadPresets();
      setSelectedPresetId("");
    } catch (err) {
      setError(`Failed to delete preset: ${String(err)}`);
    }
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const res = await fetch(`/api/tasks/sweep`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          task_id: taskId,
          append_to_task: true,
          configs: [{ agent, model, n_trials: 1 }],
          user: "probe-ui",
          extra_instructions: extraInstructions,
          probe_name: selectedPreset?.name ?? null,
          result_focus: result_focus.trim() || null,
          evaluation_metric: selectedPreset?.evaluation_metric ?? null,
        }),
      });
      if (!res.ok) {
        const body = await res.text();
        throw new Error(`HTTP ${res.status}: ${body.slice(0, 300)}`);
      }
      const data = await res.json();
      const trialId = data.new_trial_ids?.[0];
      if (scope === "experiment") {
        onSubmitted?.();
      } else if (trialId) {
        router.push(`/tasks/${taskId}/probe/${trialId}`);
      } else {
        router.refresh();
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  if (!formOpen) {
    return (
      <Button
        type="button"
        onClick={() => setFormOpen(true)}
        className="bg-blue-600 text-white hover:bg-blue-700"
      >
        Submit a probe run
      </Button>
    );
  }

  return (
    <form onSubmit={onSubmit} className="space-y-4">
      <div className="flex flex-wrap items-end gap-2">
        <label className="min-w-[200px] flex-1">
          <span className="text-sm font-medium">Your probe agents</span>
          <Select
            value={selectedPresetId || undefined}
            onValueChange={loadPreset}
          >
            <SelectTrigger className="mt-1 w-full">
              <SelectValue
                placeholder={
                  presetsLoaded
                    ? "— Select a probe agent —"
                    : "Loading presets…"
                }
              />
            </SelectTrigger>
            <SelectContent>
              {presets.map((p) => (
                <SelectItem key={p.id} value={p.id}>
                  {p.name}
                  {p.is_seed ? " (built-in)" : ""}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </label>
        <Button type="button" variant="outline" onClick={openCreateModal}>
          + Create your own probe agent
        </Button>
        {selectedPreset ? (
          <Button type="button" variant="outline" onClick={openEditModal}>
            Edit
          </Button>
        ) : null}
        {selectedPreset && !selectedPreset.is_seed ? (
          <Button
            type="button"
            variant="outline"
            onClick={() => void deleteSelectedPreset()}
            className="border-red-500/50 text-red-600 hover:bg-red-500/10 hover:text-red-600"
          >
            Delete
          </Button>
        ) : null}
      </div>
      <Dialog open={modalOpen} onOpenChange={setModalOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>
              {editingPresetId === null
                ? "Create a probe agent"
                : selectedPreset?.is_seed
                  ? "Fork a built-in probe agent"
                  : "Edit probe agent"}
            </DialogTitle>
            {selectedPreset?.is_seed && editingPresetId !== null ? (
              <p className="text-muted-foreground text-xs">
                Built-in presets can&apos;t be modified directly. Saving will
                create a personal copy you can edit and delete freely.
              </p>
            ) : null}
          </DialogHeader>
          <div className="space-y-4">
            <label className="block">
              <span className="text-sm font-medium">Name</span>
              <Input
                type="text"
                value={modalName}
                onChange={(e) => setModalName(e.target.value)}
                maxLength={80}
                className="mt-1"
                placeholder="My adversarial probe"
              />
            </label>
            <div className="flex gap-3">
              <label className="flex-1">
                <span className="text-sm font-medium">Agent</span>
                <Select
                  value={modalAgent}
                  onValueChange={(a) => {
                    setModalAgent(a);
                    setModalModel(MODELS_BY_AGENT[a]?.[0]?.value ?? "");
                  }}
                >
                  <SelectTrigger className="mt-1 w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {AGENTS.map((a) => (
                      <SelectItem key={a.value} value={a.value}>
                        {a.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </label>
              <label className="flex-1">
                <span className="text-sm font-medium">Model</span>
                <Select value={modalModel} onValueChange={setModalModel}>
                  <SelectTrigger className="mt-1 w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {(MODELS_BY_AGENT[modalAgent] ?? []).map((m) => (
                      <SelectItem key={m.value} value={m.value}>
                        {m.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </label>
            </div>
            <label className="block">
              <span className="text-sm font-medium">Operator prompt</span>
              <textarea
                value={modalOperatorPrompt}
                onChange={(e) => setModalOperatorPrompt(e.target.value)}
                rows={10}
                className="bg-background mt-1 w-full rounded border px-2 py-1.5 font-mono text-sm"
                placeholder="What should the probe agent do?"
              />
            </label>
            <label className="block">
              <span className="text-sm font-medium">
                Result focus{" "}
                <span className="text-muted-foreground">(optional)</span>
              </span>
              <p className="text-muted-foreground text-xs">
                Plain text = a question answered in prose. A JSON Schema =
                structured JSON output.
              </p>
              <ResultFocusTextarea
                value={modalResultFocus}
                onChange={setModalResultFocus}
                rows={2}
                placeholder="A specific question for the analyzer to answer"
              />
            </label>
            <label className="block">
              <span className="text-sm font-medium">Evaluation metric</span>
              <p className="text-muted-foreground text-xs">
                How should this probe&apos;s results be summarized in the
                history table?
              </p>
              <Select
                value={modalEvaluationMetric}
                onValueChange={(v) =>
                  setModalEvaluationMetric(v as EvaluationMetric)
                }
              >
                <SelectTrigger className="mt-1 w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">None — show raw reward</SelectItem>
                  <SelectItem value="result_focus">
                    Result focus — show the analyzer&apos;s answer to my
                    question
                  </SelectItem>
                </SelectContent>
              </Select>
            </label>
          </div>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setModalOpen(false)}
            >
              Cancel
            </Button>
            <Button
              type="button"
              onClick={() => void savePresetFromModal()}
              disabled={!modalName.trim() || !modalOperatorPrompt.trim()}
            >
              Save
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      {selectedPresetId ? (
        <>
          {selectedPreset?.evaluation_metric === "result_focus" ? (
            <div className="bg-muted/30 text-muted-foreground rounded border px-3 py-2 text-xs">
              <span className="text-foreground font-medium">
                Result column will show:
              </span>{" "}
              the analyzer's answer to your focus question.
            </div>
          ) : (
            <div className="bg-muted/30 text-muted-foreground rounded border px-3 py-2 text-xs">
              <span className="text-foreground font-medium">
                Result column will show:
              </span>{" "}
              raw verifier reward (no specific evaluation metric).
            </div>
          )}
          <div className="flex gap-4">
            <label className="flex-1">
              <span className="text-sm font-medium">Agent</span>
              <Select
                value={agent}
                onValueChange={(a) => {
                  setAgent(a);
                  setModel(MODELS_BY_AGENT[a][0].value);
                }}
              >
                <SelectTrigger className="mt-1 w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {AGENTS.map((a) => (
                    <SelectItem key={a.value} value={a.value}>
                      {a.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </label>
            <label className="flex-1">
              <span className="text-sm font-medium">Model</span>
              <Select value={model} onValueChange={setModel}>
                <SelectTrigger className="mt-1 w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(MODELS_BY_AGENT[agent] ?? []).map((m) => (
                    <SelectItem key={m.value} value={m.value}>
                      {m.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </label>
          </div>
          <label className="block">
            <span className="text-sm font-medium">Extra instructions</span>
            <textarea
              value={extraInstructions}
              onChange={(e) => setExtraInstructions(e.target.value)}
              placeholder="You are a security researcher. Find any way to make the verifier pass without solving the task..."
              rows={10}
              required
              className="bg-background mt-1 w-full rounded border px-2 py-1.5 font-mono text-sm"
            />
          </label>
          <label className="block">
            <span className="text-sm font-medium">
              Result focus{" "}
              <span className="text-muted-foreground">(optional)</span>
            </span>
            <p className="text-muted-foreground text-xs">
              A specific question you want the analyzer to answer about this
              trial. Shows as a callout on the result page.
            </p>
            <p className="text-muted-foreground text-xs">
              Plain text = a question answered in prose. A JSON Schema =
              structured JSON output.
            </p>
            <ResultFocusTextarea
              value={result_focus}
              onChange={setResultFocus}
              placeholder="e.g. Did the agent find any ambiguities in the spec? Or paste a JSON Schema for structured output."
            />
          </label>
          {error && (
            <p className="text-sm break-words whitespace-pre-wrap text-red-500">
              {error}
            </p>
          )}
          <Button
            type="submit"
            disabled={submitting || !extraInstructions.trim()}
            className="bg-blue-600 text-white hover:bg-blue-700"
          >
            {submitting ? "Submitting probe run…" : "Submit probe run"}
          </Button>
        </>
      ) : (
        <p className="text-muted-foreground text-sm">
          Select a probe agent above or create your own to get started.
        </p>
      )}
    </form>
  );
}

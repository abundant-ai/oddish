"use client";

import { useCallback, useEffect, useState } from "react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

type EvaluationMetric = "result_focus" | "none";

type Preset = {
  id: string;
  name: string;
  agent: string;
  model: string;
  operator_prompt: string;
  result_focus: string | null;
  evaluation_metric: EvaluationMetric | "cheat_ratio" | "ratio";
  is_seed: boolean;
  created_at: string;
  updated_at: string;
};

function ResultFocusInput({
  id,
  value,
  onChange,
}: {
  id?: string;
  value: string;
  onChange: (v: string) => void;
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
    <div className="relative">
      <Input
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onBlur={handleBlur}
        className="h-8 border-[#6f88b4]/20 pr-28"
      />
      {isSchema && (
        <span className="pointer-events-none absolute inset-y-0 right-2 flex items-center">
          <span className="rounded bg-blue-500/10 px-1.5 py-0.5 text-[10px] font-medium text-blue-600">
            structured output
          </span>
        </span>
      )}
    </div>
  );
}

// Legacy "cheat_ratio" and "ratio" both fall back to "none".
function normalizeMetric(p: Preset): EvaluationMetric {
  if (p.evaluation_metric === "cheat_ratio" || p.evaluation_metric === "ratio")
    return "none";
  return p.evaluation_metric;
}

interface FormProps {
  editing: Preset | null;
  onSaved: () => void;
  onCancel: () => void;
}

function PresetForm({ editing, onSaved, onCancel }: FormProps) {
  const [name, setName] = useState(editing?.name ?? "");
  const [agent, setAgent] = useState(editing?.agent ?? "claude-code");
  const [model, setModel] = useState(editing?.model ?? "");
  const [operatorPrompt, setOperatorPrompt] = useState(
    editing?.operator_prompt ?? "",
  );
  const [resultFocus, setResultFocus] = useState(editing?.result_focus ?? "");
  const [metric, setMetric] = useState<EvaluationMetric>(
    editing ? normalizeMetric(editing) : "none",
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Editing an existing custom preset updates in place (PUT). Creating new —
  // or forking a seed — creates a fresh preset (POST).
  const isEditCustom = editing !== null && !editing.is_seed;

  async function handleSave() {
    if (!name.trim() || !operatorPrompt.trim()) {
      setError("Name and operator prompt are required.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const payload = {
        name: name.trim(),
        agent: agent.trim(),
        model: model.trim(),
        operator_prompt: operatorPrompt,
        result_focus: resultFocus.trim() || null,
        evaluation_metric: metric === "none" ? null : metric,
      };
      const url = isEditCustom
        ? `/api/probe-presets/${editing.id}`
        : "/api/probe-presets";
      const method = isEditCustom ? "PUT" : "POST";
      const res = await fetch(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        let detail = "Failed to save preset";
        try {
          const data = (await res.json()) as { detail?: string };
          detail = data.detail ?? detail;
        } catch {
          // keep default
        }
        setError(detail);
        return;
      }
      onSaved();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card className="border-[#6f88b4]/20 shadow-xs">
      <CardHeader className="pb-3">
        <CardTitle className="text-base">
          {isEditCustom
            ? `Edit preset: ${editing.name}`
            : editing
              ? `New preset from: ${editing.name}`
              : "New preset"}
        </CardTitle>
        {editing && !isEditCustom && (
          <p className="text-[11px] text-muted-foreground">
            “{editing.name}” is a built-in default. Saving creates your own
            editable copy — the original is left unchanged.
          </p>
        )}
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="preset-name" className="text-xs font-medium">
              Name
            </Label>
            <Input
              id="preset-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="cheat-probe"
              maxLength={80}
              className="h-8 border-[#6f88b4]/20"
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="preset-agent" className="text-xs font-medium">
              Agent
            </Label>
            <Input
              id="preset-agent"
              value={agent}
              onChange={(e) => setAgent(e.target.value)}
              placeholder="claude-code"
              className="h-8 border-[#6f88b4]/20"
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="preset-model" className="text-xs font-medium">
              Model
            </Label>
            <Input
              id="preset-model"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder="anthropic/claude-sonnet-4-6"
              className="h-8 border-[#6f88b4]/20"
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="preset-metric" className="text-xs font-medium">
              Evaluation metric
            </Label>
            <Select
              value={metric}
              onValueChange={(v) => setMetric(v as EvaluationMetric)}
            >
              <SelectTrigger
                id="preset-metric"
                className="h-8 border-[#6f88b4]/20"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="none">None</SelectItem>
                <SelectItem value="result_focus">Result focus</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>


        <div className="space-y-1.5">
          <Label htmlFor="preset-prompt" className="text-xs font-medium">
            Operator prompt
          </Label>
          <textarea
            id="preset-prompt"
            value={operatorPrompt}
            onChange={(e) => setOperatorPrompt(e.target.value)}
            rows={8}
            placeholder="Prompt prepended to the task instruction…"
            className="w-full rounded-md border border-[#6f88b4]/20 bg-background px-3 py-2 font-mono text-sm resize-y focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="preset-focus" className="text-xs font-medium">
            Result focus{" "}
            <span className="text-muted-foreground">(optional)</span>
          </Label>
          <p className="text-[11px] text-muted-foreground">
            Plain text = a question answered in prose. A JSON Schema =
            structured JSON output.
          </p>
          <ResultFocusInput
            id="preset-focus"
            value={resultFocus}
            onChange={setResultFocus}
          />
        </div>

        {error && (
          <Alert variant="destructive">
            <AlertTitle>Error</AlertTitle>
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        <div className="flex items-center gap-2 pt-1">
          <Button
            type="button"
            size="sm"
            onClick={() => void handleSave()}
            disabled={saving || !name.trim() || !operatorPrompt.trim()}
            className="h-8"
          >
            {saving
              ? "Saving…"
              : isEditCustom
                ? "Update preset"
                : "Create preset"}
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-8 border-[#6f88b4]/20"
            onClick={onCancel}
            disabled={saving}
          >
            Cancel
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

export function PresetsClient() {
  const [presets, setPresets] = useState<Preset[]>([]);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [formState, setFormState] = useState<"new" | Preset | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setFetchError(null);
    try {
      const res = await fetch("/api/probe-presets");
      if (!res.ok) {
        setFetchError(`Failed to load presets (HTTP ${res.status})`);
        return;
      }
      // Includes built-in default presets (read-only) plus the org's own.
      setPresets((await res.json()) as Preset[]);
    } catch (err: unknown) {
      setFetchError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function handleDelete(preset: Preset) {
    if (!confirm(`Delete preset "${preset.name}"? This cannot be undone.`))
      return;
    try {
      const res = await fetch(`/api/probe-presets/${preset.id}`, {
        method: "DELETE",
      });
      if (!res.ok) {
        alert(`Failed to delete preset (HTTP ${res.status})`);
        return;
      }
      await load();
    } catch (err: unknown) {
      alert(err instanceof Error ? err.message : String(err));
    }
  }

  function handleSaved() {
    setFormState(null);
    void load();
  }

  if (formState !== null) {
    return (
      <div className="space-y-4">
        <PresetForm
          editing={formState === "new" ? null : formState}
          onSaved={handleSaved}
          onCancel={() => setFormState(null)}
        />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <Card className="border-[#6f88b4]/20 shadow-xs">
        <CardHeader className="flex flex-col gap-3 pb-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="space-y-1">
            <CardTitle className="text-base">Probe presets</CardTitle>
            <p className="text-[11px] text-muted-foreground">
              Reusable probe configurations available when launching a run.
            </p>
          </div>
          <Button
            type="button"
            size="sm"
            className="h-8 text-xs"
            onClick={() => setFormState("new")}
          >
            + New preset
          </Button>
        </CardHeader>
        <CardContent>
          {fetchError ? (
            <Alert variant="destructive">
              <AlertTitle>Failed to load presets</AlertTitle>
              <AlertDescription>{fetchError}</AlertDescription>
            </Alert>
          ) : loading ? (
            <div className="py-8 text-center text-sm text-muted-foreground">
              Loading…
            </div>
          ) : presets.length === 0 ? (
            <div className="rounded-lg border border-dashed border-[#6f88b4]/30 bg-card/60 px-6 py-10 text-center text-sm text-muted-foreground">
              No presets yet. Create one to get started.
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-[180px]">Name</TableHead>
                  <TableHead className="w-[140px]">Agent</TableHead>
                  <TableHead>Model</TableHead>
                  <TableHead className="w-[90px] text-right"></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {presets.map((p) => (
                  <TableRow key={p.id}>
                    <TableCell className="font-mono text-xs font-medium">
                      <button
                        type="button"
                        onClick={() => setFormState(p)}
                        className="text-left hover:underline"
                      >
                        {p.name}
                      </button>
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {p.agent}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {p.model}
                    </TableCell>
                    <TableCell className="text-right">
                      {/* Click the name to open. Built-in defaults open as a new
                          copy and can't be deleted; your own presets can. */}
                      {!p.is_seed && (
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          className="h-6 px-2 text-[11px] text-red-500 hover:text-red-600 hover:bg-red-500/10"
                          onClick={() => void handleDelete(p)}
                        >
                          Delete
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

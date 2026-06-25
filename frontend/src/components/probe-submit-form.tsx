"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
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

type Skill = {
  id: string;
  name: string;
  is_seed: boolean;
  operator_prompt: string | null;
  result_focus: string | null;
  evaluation_metric: string | null;
  files: { relative_path: string; content: string }[];
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

  const [skills, setSkills] = useState<Skill[]>([]);
  const [skillsLoaded, setSkillsLoaded] = useState(false);
  const [selectedSkillId, setSelectedSkillId] = useState<string>("");
  const [result_focus, setResultFocus] = useState("");

  const selectedSkill = skills.find((s) => s.id === selectedSkillId) ?? null;

  const reloadSkills = useCallback(async () => {
    try {
      const res = await fetch(`/api/skills`, { cache: "no-store" });
      if (!res.ok) {
        console.warn("skills fetch failed:", res.status);
        return;
      }
      const data = await res.json();
      if (!Array.isArray(data)) return;
      setSkills(data as Skill[]);
    } catch (err) {
      console.warn("skills fetch error:", err);
    } finally {
      setSkillsLoaded(true);
    }
  }, []);

  useEffect(() => {
    void reloadSkills();
  }, [reloadSkills]);

  function loadSkill(id: string) {
    setSelectedSkillId(id);
    if (!id) return;
    const s = skills.find((x) => x.id === id);
    if (!s) return;
    setExtraInstructions(s.operator_prompt ?? "");
    setResultFocus(s.result_focus ?? "");
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
          probe_name: selectedSkill?.name ?? null,
          result_focus: result_focus.trim() || null,
          evaluation_metric: selectedSkill?.evaluation_metric ?? null,
          skill_ids: selectedSkillId ? [selectedSkillId] : null,
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

  return (
    <form onSubmit={onSubmit} className="space-y-4">
      <div className="flex flex-wrap items-end gap-2">
        <label className="min-w-[200px] flex-1">
          <span className="text-sm font-medium">Skill</span>
          <Select
            value={selectedSkillId || undefined}
            onValueChange={loadSkill}
          >
            <SelectTrigger className="mt-1 w-full">
              <SelectValue
                placeholder={
                  skillsLoaded ? "— Select a skill —" : "Loading skills…"
                }
              />
            </SelectTrigger>
            <SelectContent>
              {skills.map((s) => (
                <SelectItem key={s.id} value={s.id}>
                  {s.name}
                  {!s.operator_prompt ? " (bundle)" : ""}
                  {s.is_seed ? " (built-in)" : ""}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </label>
        <Button type="button" variant="outline" asChild>
          <a href="/qa/skills" target="_blank" rel="noreferrer">
            Manage skills
          </a>
        </Button>
      </div>
      {selectedSkillId ? (
        <>
          {selectedSkill?.evaluation_metric === "result_focus" ? (
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
          Select a skill above to get started.
        </p>
      )}
    </form>
  );
}

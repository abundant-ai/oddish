"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@clerk/nextjs";

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

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8800";

export function FreeformSubmitForm({ taskId }: { taskId: string }) {
  const router = useRouter();
  const { getToken } = useAuth();
  const [agent, setAgent] = useState("claude-code");
  const [model, setModel] = useState(MODELS_BY_AGENT["claude-code"][0].value);
  const [extraInstructions, setExtraInstructions] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      // Match the codebase's getClerkToken pattern: prefer the configured
      // template (CLERK_JWT_TEMPLATE=oddish in .env.local), fall back to
      // the default session token if the template is missing.
      let token: string | null = null;
      try {
        token = await getToken({ template: "oddish" });
      } catch (err) {
        console.warn(
          "Failed to get Clerk token for template 'oddish', falling back to session token.",
          err,
        );
      }
      if (!token) {
        token = await getToken();
      }

      const res = await fetch(`${API_URL}/tasks/sweep`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({
          task_id: taskId,
          append_to_task: true,
          configs: [{ agent, model, n_trials: 1 }],
          user: "freeform-ui",
          extra_instructions: extraInstructions,
        }),
      });
      if (!res.ok) {
        const body = await res.text();
        throw new Error(`HTTP ${res.status}: ${body.slice(0, 300)}`);
      }
      const data = await res.json();
      const trialId = data.new_trial_ids?.[0];
      if (trialId) router.push(`/tasks/${taskId}/freeform-agent/${trialId}`);
      else router.refresh();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="space-y-4">
      <div className="flex gap-4">
        <label className="flex-1">
          <span className="text-sm font-medium">Agent</span>
          <select
            value={agent}
            onChange={(e) => {
              const a = e.target.value;
              setAgent(a);
              setModel(MODELS_BY_AGENT[a][0].value);
            }}
            className="mt-1 w-full rounded border bg-background px-2 py-1.5 text-sm"
          >
            {AGENTS.map((a) => (
              <option key={a.value} value={a.value}>
                {a.label}
              </option>
            ))}
          </select>
        </label>
        <label className="flex-1">
          <span className="text-sm font-medium">Model</span>
          <select
            value={model}
            onChange={(e) => setModel(e.target.value)}
            className="mt-1 w-full rounded border bg-background px-2 py-1.5 text-sm"
          >
            {(MODELS_BY_AGENT[agent] ?? []).map((m) => (
              <option key={m.value} value={m.value}>
                {m.label}
              </option>
            ))}
          </select>
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
          className="mt-1 w-full rounded border bg-background px-2 py-1.5 font-mono text-sm"
        />
      </label>
      {error && (
        <p className="text-sm text-red-500 break-words whitespace-pre-wrap">
          {error}
        </p>
      )}
      <button
        type="submit"
        disabled={submitting || !extraInstructions.trim()}
        className="rounded bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
      >
        {submitting ? "Submitting..." : "Submit"}
      </button>
    </form>
  );
}

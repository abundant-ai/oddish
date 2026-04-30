"use client";

import { use, useEffect, useState } from "react";
import useSWR from "swr";
import Link from "next/link";
import { useAuth } from "@clerk/nextjs";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8800";

type AgentMessage = {
  kind: "assistant_text" | "tool_result" | "result";
  text: string;
  is_error?: boolean;
};

type FreeformSummary = {
  kind?: string;
  headline?: string;
  summary?: string;
  key_actions?: string[];
  cheating_attempted?: boolean | null;
  cheating_succeeded?: boolean | null;
  evidence?: string;
  model?: string;
  generated_at?: string;
};

type Trial = {
  id: string;
  agent: string;
  model: string | null;
  status: string;
  reward: number | null;
  started_at: string | null;
  finished_at: string | null;
  harbor_config: { mode?: string; extra_instructions?: string } | null;
  result: {
    _artifacts?: {
      trajectory?: unknown;
      verifier_stdout?: string;
      agent_messages?: AgentMessage[];
    };
  } | null;
  analysis: FreeformSummary | null;
  analysis_status: string | null;
  analysis_error: string | null;
  error_message: string | null;
};

export default function FreeformResultPage({
  params,
}: {
  params: Promise<{ task_id: string; trial_id: string }>;
}) {
  const { task_id, trial_id } = use(params);
  const { getToken } = useAuth();

  const fetcher = async (url: string) => {
    let token: string | null = null;
    try {
      token = await getToken({ template: "oddish" });
    } catch {
      token = await getToken();
    }
    const res = await fetch(url, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!res.ok)
      throw new Error(`HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`);
    return res.json();
  };

  const { data: trials, error } = useSWR<Trial[]>(
    `${API_URL}/tasks/${task_id}/trials`,
    fetcher,
    {
      refreshInterval: (data) => {
        const t = (data ?? []).find((x) => x.id === trial_id);
        return t && (t.status === "success" || t.status === "failed")
          ? 0
          : 3000;
      },
    },
  );

  // After ~30s of polling without finding the trial, treat it as truly missing.
  // Until then, keep showing a loading state — covers the race where the trial
  // row was just created and SWR's first fetch was served from stale cache.
  const [waitedTooLong, setWaitedTooLong] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setWaitedTooLong(true), 30_000);
    return () => clearTimeout(t);
  }, [trial_id]);

  if (error)
    return (
      <div className="container mx-auto max-w-3xl py-8">
        <p className="text-sm text-red-500">
          Failed to load trial: {error.message}
        </p>
      </div>
    );
  if (!trials)
    return (
      <div className="container mx-auto max-w-3xl py-8">
        <p className="text-sm text-muted-foreground">Loading…</p>
      </div>
    );
  const trial = trials.find((t) => t.id === trial_id);
  if (!trial) {
    if (waitedTooLong) {
      return (
        <div className="container mx-auto max-w-3xl py-8">
          <p className="text-sm text-red-500">Trial not found.</p>
          <Link
            href={`/tasks/${task_id}/freeform-agent`}
            className="text-xs underline text-muted-foreground hover:text-foreground"
          >
            ← Back to workbench
          </Link>
        </div>
      );
    }
    return (
      <div className="container mx-auto max-w-3xl py-8">
        <p className="text-sm text-muted-foreground">
          Waiting for the trial to register…
        </p>
      </div>
    );
  }

  const extra = trial.harbor_config?.extra_instructions ?? "";
  const summary = trial.analysis;
  const artifacts = trial.result?._artifacts;
  const messages = artifacts?.agent_messages ?? [];
  const verifierStdout = artifacts?.verifier_stdout;
  const cheatFound =
    trial.reward !== null && trial.reward !== undefined && trial.reward >= 0.5;

  return (
    <div className="container mx-auto max-w-4xl py-8 space-y-6">
      <div>
        <Link
          href={`/tasks/${task_id}/freeform-agent`}
          className="text-xs underline text-muted-foreground hover:text-foreground"
        >
          ← Back to workbench
        </Link>
        <h1 className="mt-2 text-2xl font-semibold">Freeform run</h1>
        <p className="mt-1 font-mono text-xs text-muted-foreground">
          {trial.id}
        </p>
      </div>

      {/* Status header */}
      <section className="rounded border p-4">
        <div className="flex flex-wrap items-center gap-3 text-sm">
          <span className="inline-block rounded bg-muted px-2 py-1 text-xs font-mono">
            {trial.status}
          </span>
          <span>
            agent: <code className="text-xs">{trial.agent}</code>
          </span>
          {trial.model ? (
            <span>
              model: <code className="text-xs">{trial.model}</code>
            </span>
          ) : null}
          {trial.reward !== null && trial.reward !== undefined ? (
            <span>
              reward: <strong>{trial.reward.toFixed(2)}</strong>
            </span>
          ) : null}
          {trial.reward !== null && trial.reward !== undefined ? (
            cheatFound ? (
              <span className="rounded bg-red-500/15 px-2 py-1 text-xs font-medium text-red-600">
                Cheat may have succeeded
              </span>
            ) : (
              <span className="rounded bg-emerald-500/15 px-2 py-1 text-xs font-medium text-emerald-700">
                Verifier failed (reward &lt; 0.5)
              </span>
            )
          ) : null}
        </div>
        {trial.error_message ? (
          <p className="mt-2 text-sm text-red-500 break-words whitespace-pre-wrap">
            {trial.error_message}
          </p>
        ) : null}
      </section>

      {/* LLM Summary */}
      {summary ? (
        <section className="rounded border-2 border-primary/30 bg-primary/5 p-4 space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Summary
            </h2>
            <span className="text-[10px] text-muted-foreground">
              {summary.model ?? ""}
            </span>
          </div>
          {summary.headline ? (
            <p className="text-base font-medium leading-snug">
              {summary.headline}
            </p>
          ) : null}
          {summary.summary ? (
            <p className="text-sm leading-relaxed">{summary.summary}</p>
          ) : null}
          {summary.key_actions && summary.key_actions.length > 0 ? (
            <div>
              <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground mb-1">
                Key actions
              </p>
              <ul className="list-disc pl-5 space-y-1 text-sm">
                {summary.key_actions.map((a, i) => (
                  <li key={i}>{a}</li>
                ))}
              </ul>
            </div>
          ) : null}
          {summary.cheating_attempted !== null &&
          summary.cheating_attempted !== undefined ? (
            <div className="flex gap-3 text-xs">
              <span className="rounded bg-muted px-2 py-1">
                cheating attempted:{" "}
                <strong>{String(summary.cheating_attempted)}</strong>
              </span>
              {summary.cheating_succeeded !== null &&
              summary.cheating_succeeded !== undefined ? (
                <span className="rounded bg-muted px-2 py-1">
                  cheating succeeded:{" "}
                  <strong>{String(summary.cheating_succeeded)}</strong>
                </span>
              ) : null}
            </div>
          ) : null}
          {summary.evidence ? (
            <p className="text-xs text-muted-foreground italic">
              Evidence: {summary.evidence}
            </p>
          ) : null}
        </section>
      ) : trial.analysis_status === "FAILED" ||
        trial.analysis_status === "failed" ? (
        <section className="rounded border p-4">
          <p className="text-xs text-red-500">
            Summary failed: {trial.analysis_error ?? "(no detail)"}
          </p>
        </section>
      ) : trial.status === "running" ||
        trial.status === "queued" ||
        trial.status === "pending" ? (
        <section className="rounded border p-4">
          <p className="text-xs text-muted-foreground">
            Summary will appear once the trial completes.
          </p>
        </section>
      ) : null}

      {/* Operator instructions */}
      <section className="rounded border p-4 space-y-2">
        <h2 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Operator instructions
        </h2>
        <pre className="whitespace-pre-wrap rounded bg-muted p-3 font-mono text-xs">
          {extra || "(none)"}
        </pre>
      </section>

      {/* Agent thought process */}
      <section className="rounded border p-4 space-y-3">
        <h2 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Agent thought process
        </h2>
        {messages.length === 0 ? (
          <p className="text-xs text-muted-foreground">(no messages yet)</p>
        ) : (
          <ol className="space-y-3 text-sm">
            {messages.map((m, i) => (
              <li key={i} className="border-l-2 border-muted pl-3">
                <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-1">
                  Step {i + 1} · {m.kind}
                </div>
                <div
                  className={`whitespace-pre-wrap font-mono text-xs ${m.is_error ? "text-red-500" : ""}`}
                >
                  {m.text}
                </div>
              </li>
            ))}
          </ol>
        )}
      </section>

      {/* Verifier output */}
      <section className="rounded border p-4 space-y-2">
        <h2 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Verifier output
        </h2>
        <pre className="overflow-auto whitespace-pre-wrap rounded bg-muted p-3 font-mono text-xs max-h-[400px]">
          {verifierStdout || "(empty)"}
        </pre>
      </section>

      {/* Raw JSON (collapsed) */}
      <section className="rounded border p-4">
        <details>
          <summary className="cursor-pointer text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Raw trial.result JSON
          </summary>
          <pre className="mt-2 overflow-auto rounded bg-muted p-3 font-mono text-xs max-h-[600px]">
            {JSON.stringify(trial.result ?? {}, null, 2)}
          </pre>
        </details>
      </section>
    </div>
  );
}

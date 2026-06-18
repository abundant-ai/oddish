"use client";

import { use, useEffect, useState } from "react";
import useSWR from "swr";
import Link from "next/link";
import { type ProbeSummary } from "@/lib/probe-summary";
import { ProbeRunSummary } from "@/components/probe-run-summary";

type AgentMessage = {
  kind: "assistant_text" | "tool_use" | "tool_result" | "result";
  text: string;
  name?: string; // tool name when kind === "tool_use" (e.g. "Bash", "Read")
  is_error?: boolean;
};

type Artifacts = {
  trajectory?: unknown;
  verifier_stdout?: string;
  agent_messages?: AgentMessage[];
};

function kindLabel(m: AgentMessage): string {
  if (m.kind === "tool_use") return m.name ? `tool: ${m.name}` : "tool call";
  if (m.kind === "tool_result") return "tool result";
  if (m.kind === "assistant_text") return "agent text";
  if (m.kind === "result") return "final result";
  return m.kind;
}

type Trial = {
  id: string;
  agent: string;
  model: string | null;
  status: string;
  reward: number | null;
  started_at: string | null;
  finished_at: string | null;
  harbor_config: {
    mode?: string;
    extra_instructions?: string;
    // "cheat_ratio" kept as legacy alias — read sites normalize it.
    evaluation_metric?: "ratio" | "result_focus" | "none" | "cheat_ratio";
    ratio_unit?: string | null;
    ratio_verb?: string | null;
  } | null;
  result: {
    _artifacts?: Artifacts;
  } | null;
  analysis: ProbeSummary | null;
  analysis_status: string | null;
  analysis_error: string | null;
  error_message: string | null;
};

export default function ProbeResultPage({
  params,
}: {
  params: Promise<{ task_id: string; trial_id: string }>;
}) {
  const { task_id, trial_id } = use(params);
  const fetcher = async (url: string) => {
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok)
      throw new Error(
        `HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`,
      );
    return res.json();
  };

  const { data: trials, error } = useSWR<Trial[]>(
    `/api/tasks/${task_id}/trials`,
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

  // Cloud trials don't inline `_artifacts` into `result` (only the local runner
  // does); the agent transcript + verifier output live in object storage. When
  // a finished trial has no inlined artifacts, pull them on demand so the panels
  // below render the real output instead of "(no messages yet)".
  const candidate = (trials ?? []).find((t) => t.id === trial_id);
  const needsArtifacts =
    !!candidate &&
    !candidate.result?._artifacts &&
    (candidate.status === "success" || candidate.status === "failed");
  const { data: fetchedArtifacts } = useSWR<Artifacts>(
    needsArtifacts ? `/api/trials/${trial_id}/probe-artifacts` : null,
    fetcher,
  );

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
            href={`/tasks/${task_id}/probe`}
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
  const artifacts = trial.result?._artifacts ?? fetchedArtifacts;
  const messages = artifacts?.agent_messages ?? [];
  const verifierStdout = artifacts?.verifier_stdout;

  return (
    <div className="container mx-auto max-w-4xl py-8 space-y-6">
      <div>
        <Link
          href={`/tasks/${task_id}/probe`}
          className="text-xs underline text-muted-foreground hover:text-foreground"
        >
          ← Back to workbench
        </Link>
        <p className="mt-2 text-xs uppercase tracking-wide text-muted-foreground">
          Probe run
        </p>
        <p className="text-sm">
          Task:{" "}
          <Link
            href={`/tasks/${task_id}`}
            className="font-mono font-medium text-blue-600 underline-offset-4 hover:text-blue-700 hover:underline"
          >
            {task_id}
          </Link>
        </p>
        <p className="mt-1 font-mono text-xs text-muted-foreground">
          {trial.id}
        </p>
      </div>

      {trial.error_message ? (
        <section className="rounded border p-4">
          <p className="text-sm text-red-500 break-words whitespace-pre-wrap">
            {trial.error_message}
          </p>
        </section>
      ) : null}

      <ProbeRunSummary trial={trial} />

      {/* Agent process */}
      <section className="rounded border p-4 space-y-3">
        <h2 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Agent process
        </h2>
        {messages.length === 0 ? (
          <p className="text-xs text-muted-foreground">(no messages yet)</p>
        ) : summary?.attempts && summary.attempts.length > 0 ? (
          (() => {
            const claimed = new Set<number>();
            for (const a of summary.attempts ?? []) {
              for (const idx of a.step_indices ?? []) {
                if (
                  Number.isInteger(idx) &&
                  idx >= 0 &&
                  idx < messages.length
                ) {
                  claimed.add(idx);
                }
              }
            }
            const ungrouped = messages
              .map((m, i) => ({ m, i }))
              .filter(({ i }) => !claimed.has(i));
            return (
              <div className="space-y-3">
                {(summary.attempts ?? []).map((attempt, attemptIdx) => {
                  const indices = (attempt.step_indices ?? []).filter(
                    (idx) =>
                      Number.isInteger(idx) &&
                      idx >= 0 &&
                      idx < messages.length,
                  );
                  return (
                    <details
                      key={attemptIdx}
                      className="rounded border-2 border-primary/20 bg-primary/5 p-3"
                    >
                      <summary className="cursor-pointer">
                        <span className="text-sm font-medium">
                          Attempt {attemptIdx + 1}:{" "}
                          {attempt.title || "(untitled)"}
                        </span>
                        {attempt.success === true ? (
                          <span className="ml-2 rounded bg-red-500/15 px-1.5 py-0.5 text-[10px] font-medium text-red-600">
                            cheat succeeded
                          </span>
                        ) : attempt.success === false ? (
                          <span className="ml-2 rounded bg-emerald-500/15 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700">
                            blocked
                          </span>
                        ) : (
                          <span
                            className="ml-2 rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground"
                            title="Not a cheat attempt — investigation/setup steps"
                          >
                            investigation
                          </span>
                        )}
                        <span className="ml-2 text-xs text-muted-foreground">
                          ({indices.length} step
                          {indices.length === 1 ? "" : "s"})
                        </span>
                      </summary>
                      <div className="mt-3 space-y-2">
                        {attempt.rationale ? (
                          <p className="text-xs">
                            <strong>Rationale:</strong> {attempt.rationale}
                          </p>
                        ) : null}
                        {attempt.outcome ? (
                          <p className="text-xs">
                            <strong>Outcome:</strong> {attempt.outcome}
                          </p>
                        ) : null}
                        <div className="space-y-2">
                          {indices.map((idx) => {
                            const m = messages[idx];
                            return (
                              <details
                                key={idx}
                                className="rounded border bg-muted/30 p-2"
                              >
                                <summary className="cursor-pointer text-xs font-medium text-muted-foreground">
                                  Step {idx + 1} · {kindLabel(m)}
                                  {m.text ? (
                                    <span className="ml-2 font-normal text-muted-foreground/80">
                                      {m.text.slice(0, 80)}
                                      {m.text.length > 80 ? "…" : ""}
                                    </span>
                                  ) : null}
                                </summary>
                                <pre
                                  className={`mt-2 whitespace-pre-wrap font-mono text-xs ${m.is_error ? "text-red-500" : ""}`}
                                >
                                  {m.text}
                                </pre>
                              </details>
                            );
                          })}
                        </div>
                      </div>
                    </details>
                  );
                })}
                {ungrouped.length > 0 ? (
                  <div className="space-y-2">
                    <p className="text-[10px] uppercase tracking-wide text-muted-foreground">
                      Ungrouped steps
                    </p>
                    <div className="space-y-2">
                      {ungrouped.map(({ m, i }) => (
                        <details
                          key={i}
                          className="rounded border bg-muted/30 p-2"
                        >
                          <summary className="cursor-pointer text-xs font-medium text-muted-foreground">
                            Step {i + 1} · {kindLabel(m)}
                            {m.text ? (
                              <span className="ml-2 font-normal text-muted-foreground/80">
                                {m.text.slice(0, 80)}
                                {m.text.length > 80 ? "…" : ""}
                              </span>
                            ) : null}
                          </summary>
                          <pre
                            className={`mt-2 whitespace-pre-wrap font-mono text-xs ${m.is_error ? "text-red-500" : ""}`}
                          >
                            {m.text}
                          </pre>
                        </details>
                      ))}
                    </div>
                  </div>
                ) : null}
              </div>
            );
          })()
        ) : (
          <ol className="space-y-2 text-sm">
            {messages.map((m, i) => (
              <li key={i}>
                <details className="rounded border bg-muted/30 p-2">
                  <summary className="cursor-pointer text-xs font-medium text-muted-foreground">
                    Step {i + 1} · {kindLabel(m)}
                    {m.text ? (
                      <span className="ml-2 font-normal text-muted-foreground/80">
                        {m.text.slice(0, 80)}
                        {m.text.length > 80 ? "…" : ""}
                      </span>
                    ) : null}
                  </summary>
                  <pre
                    className={`mt-2 whitespace-pre-wrap font-mono text-xs ${m.is_error ? "text-red-500" : ""}`}
                  >
                    {m.text}
                  </pre>
                </details>
              </li>
            ))}
          </ol>
        )}
      </section>

      {/* Operator instructions */}
      <section className="rounded border p-4 space-y-2">
        <h2 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Operator instructions
        </h2>
        <pre className="whitespace-pre-wrap rounded bg-muted p-3 font-mono text-xs">
          {extra || "(none)"}
        </pre>
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

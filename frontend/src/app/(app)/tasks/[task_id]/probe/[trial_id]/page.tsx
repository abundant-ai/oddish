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

type Attempt = {
  title?: string;
  rationale?: string;
  outcome?: string;
  success?: boolean | null;
  step_indices?: number[];
};

type ProbeSummary = {
  kind?: string;
  headline?: string;
  summary?: string;
  key_actions?: string[];
  cheating_attempted?: boolean | null;
  cheating_succeeded?: boolean | null;
  evidence?: string;
  attempts?: Attempt[];
  model?: string;
  generated_at?: string;
  result_focus_question?: string | null;
  result_focus_findings?: string | null;
};

function pluralize(noun: string): string {
  const n = noun.trim();
  if (!n) return "";
  if (/[sxz]$|[cs]h$/.test(n)) return n + "es";
  if (/[^aeiou]y$/.test(n)) return n.slice(0, -1) + "ies";
  return n + "s";
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
    _artifacts?: {
      trajectory?: unknown;
      verifier_stdout?: string;
      agent_messages?: AgentMessage[];
    };
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
  const artifacts = trial.result?._artifacts;
  const messages = artifacts?.agent_messages ?? [];
  const verifierStdout = artifacts?.verifier_stdout;
  const cheatFound =
    trial.reward !== null && trial.reward !== undefined && trial.reward >= 0.5;

  return (
    <div className="container mx-auto max-w-4xl py-8 space-y-6">
      <div>
        <Link
          href={`/tasks/${task_id}/probe`}
          className="text-xs underline text-muted-foreground hover:text-foreground"
        >
          ← Back to workbench
        </Link>
        <h1 className="mt-2 text-2xl font-semibold">Probe run</h1>
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
            <div className="flex items-center gap-2">
              {(() => {
                const rawMetric =
                  trial.harbor_config?.evaluation_metric ?? "none";
                const metric =
                  rawMetric === "cheat_ratio" ? "ratio" : rawMetric;
                const label =
                  metric === "ratio"
                    ? "ratio metric"
                    : metric === "result_focus"
                      ? "result focus metric"
                      : "no specific metric";
                return (
                  <span className="rounded bg-muted px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                    {label}
                  </span>
                );
              })()}
              <span className="text-[10px] text-muted-foreground">
                {summary.model ?? ""}
              </span>
            </div>
          </div>
          {(() => {
            const rawMetric =
              trial.harbor_config?.evaluation_metric ?? "none";
            const metric =
              rawMetric === "cheat_ratio" ? "ratio" : rawMetric;
            // Always render the result_focus block when metric is result_focus,
            // even if findings are missing — show an "awaiting answer" placeholder.
            if (metric === "result_focus") {
              return (
                <div className="rounded border-2 border-amber-500/30 bg-amber-500/5 p-3 mb-2 space-y-2">
                  <p className="text-xs font-medium uppercase tracking-wide text-amber-700">
                    Result focus
                  </p>
                  {summary.result_focus_question ? (
                    <p className="text-sm font-medium italic">
                      {summary.result_focus_question}
                    </p>
                  ) : null}
                  {summary.result_focus_findings ? (
                    <p className="text-sm">{summary.result_focus_findings}</p>
                  ) : (
                    <p className="text-sm text-muted-foreground italic">
                      awaiting answer
                    </p>
                  )}
                </div>
              );
            }
            // Legacy path: show findings if both question and findings present
            if (
              summary.result_focus_question &&
              summary.result_focus_findings
            ) {
              return (
                <div className="rounded border-2 border-amber-500/30 bg-amber-500/5 p-3 mb-2 space-y-2">
                  <p className="text-xs font-medium uppercase tracking-wide text-amber-700">
                    Result focus
                  </p>
                  <p className="text-sm font-medium italic">
                    {summary.result_focus_question}
                  </p>
                  <p className="text-sm">{summary.result_focus_findings}</p>
                </div>
              );
            }
            return null;
          })()}
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
          {(() => {
            const rawMetric =
              trial.harbor_config?.evaluation_metric ?? "none";
            const metric =
              rawMetric === "cheat_ratio" ? "ratio" : rawMetric;
            const unit =
              trial.harbor_config?.ratio_unit ??
              (rawMetric === "cheat_ratio" ? "cheat" : "attempt");
            const verb =
              trial.harbor_config?.ratio_verb ??
              (rawMetric === "cheat_ratio" ? "succeeded" : null);
            const plural = pluralize(unit);
            const verbStr = verb ? ` ${verb}` : "";
            // For ratio probes, render the breakdown chips even when
            // attempts is missing/empty so the column shape is consistent.
            const all = summary.attempts ?? [];
            const succeeded = all.filter((a) => a.success === true).length;
            const blocked = all.filter((a) => a.success === false).length;
            const investigation = all.length - succeeded - blocked;
            const cheatTotal = succeeded + blocked;

            if (metric === "ratio") {
              return (
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <span
                    className={`rounded px-2 py-1 font-medium ${
                      succeeded > 0
                        ? "bg-red-500/15 text-red-600"
                        : "bg-muted text-muted-foreground"
                    }`}
                  >
                    {succeeded} {succeeded === 1 ? unit : plural}
                    {verbStr}
                  </span>
                  <span
                    className={`rounded px-2 py-1 font-medium ${
                      blocked > 0
                        ? "bg-emerald-500/15 text-emerald-700"
                        : "bg-muted text-muted-foreground"
                    }`}
                  >
                    {blocked} blocked
                  </span>
                  {investigation > 0 ? (
                    <span className="rounded bg-muted px-2 py-1 font-medium text-muted-foreground">
                      {investigation} investigation step
                      {investigation === 1 ? "" : "s"}
                    </span>
                  ) : null}
                  {cheatTotal > 0 ? (
                    <span className="text-muted-foreground">
                      {succeeded > 0
                        ? "task is gameable"
                        : "task is robust"}
                    </span>
                  ) : (
                    <span className="text-muted-foreground italic">
                      no {plural} identified
                    </span>
                  )}
                </div>
              );
            }

            if (cheatTotal === 0 && investigation === 0) return null;
            return (
              <div className="flex flex-wrap items-center gap-2 text-xs">
                {succeeded > 0 ? (
                  <span className="rounded bg-red-500/15 px-2 py-1 font-medium text-red-600">
                    {succeeded} {succeeded === 1 ? unit : plural}
                    {verbStr}
                  </span>
                ) : null}
                {blocked > 0 ? (
                  <span className="rounded bg-emerald-500/15 px-2 py-1 font-medium text-emerald-700">
                    {blocked} blocked
                  </span>
                ) : null}
                {investigation > 0 ? (
                  <span className="rounded bg-muted px-2 py-1 font-medium text-muted-foreground">
                    {investigation} investigation step{investigation === 1 ? "" : "s"}
                  </span>
                ) : null}
                {cheatTotal > 0 ? (
                  <span className="text-muted-foreground">
                    {succeeded > 0
                      ? "task is gameable"
                      : "task is robust"}
                  </span>
                ) : null}
              </div>
            );
          })()}
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
                                  Step {idx + 1} · {m.kind}
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
                            Step {i + 1} · {m.kind}
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
                    Step {i + 1} · {m.kind}
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

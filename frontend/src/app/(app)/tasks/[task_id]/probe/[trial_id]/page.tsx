"use client";

import { use, useEffect, useState } from "react";
import useSWR from "swr";
import Link from "next/link";
import { Search, X } from "lucide-react";
import { type ProbeSummary } from "@/lib/probe-summary";
import { ProbeRunSummary } from "@/components/probe-run-summary";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";

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
    // Operator-selected preset name; absent on older / preset-less runs.
    probe_name?: string | null;
    // "cheat_ratio"/"ratio" are legacy aliases — normalizeMetric maps them to "none".
    evaluation_metric?: "result_focus" | "none" | "cheat_ratio" | "ratio";
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

  // Keyword filter for the agent-process steps (mirrors the trials trajectory).
  const [stepQuery, setStepQuery] = useState("");

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
  const artifacts = trial.result?._artifacts ?? fetchedArtifacts;
  const messages = artifacts?.agent_messages ?? [];
  const verifierStdout = artifacts?.verifier_stdout;

  // Filter steps by keyword while keeping each step's original 1-based number.
  const q = stepQuery.trim().toLowerCase();
  const visibleSteps = messages
    .map((m, i) => ({ m, i }))
    .filter(
      ({ m }) =>
        !q ||
        kindLabel(m).toLowerCase().includes(q) ||
        (m.name ?? "").toLowerCase().includes(q) ||
        m.text.toLowerCase().includes(q),
    );

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

      <p className="text-sm">
        Preset:{" "}
        <span className="font-mono font-medium">
          {trial.harbor_config?.probe_name?.trim() || trial.agent}
        </span>
      </p>

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
        ) : (
          <>
            <div className="relative">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                type="text"
                value={stepQuery}
                onChange={(e) => setStepQuery(e.target.value)}
                placeholder="Filter steps by keyword…"
                className="h-9 pl-8 pr-8 text-sm"
              />
              {stepQuery && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => setStepQuery("")}
                  aria-label="Clear search"
                  className="absolute right-1 top-1/2 h-7 w-7 -translate-y-1/2 p-0 text-muted-foreground hover:text-foreground"
                >
                  <X className="h-3.5 w-3.5" />
                </Button>
              )}
            </div>
            {visibleSteps.length === 0 ? (
              <p className="text-xs text-muted-foreground">
                No steps match “{stepQuery}”.
              </p>
            ) : (
              <ol className="space-y-2 text-sm">
                {visibleSteps.map(({ m, i }) => (
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
          </>
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

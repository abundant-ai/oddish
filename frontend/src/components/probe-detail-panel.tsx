"use client";

import { useEffect, useState } from "react";
import useSWR from "swr";
import Link from "next/link";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { type ProbeSummary } from "@/lib/probe-summary";
import { ProbeRunSummary } from "@/components/probe-run-summary";
import { ResizableDrawer } from "@/components/ui/resizable-drawer";

type AgentMessage = {
  kind: "assistant_text" | "tool_use" | "tool_result" | "result";
  text: string;
  name?: string;
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
    probe_name?: string | null;
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

interface ProbeDetailPanelProps {
  taskId: string;
  trialId: string;
  isOpen: boolean;
  onClose: () => void;
  contentOnly?: boolean;
}

const fetcher = async (url: string) => {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok)
    throw new Error(`HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`);
  return res.json();
};

export function ProbeDetailPanel({
  taskId,
  trialId,
  isOpen,
  onClose,
  contentOnly = false,
}: ProbeDetailPanelProps) {
  // Track the currently-viewed probe; reset whenever the caller opens a
  // different one.
  const [currentId, setCurrentId] = useState(trialId);
  useEffect(() => setCurrentId(trialId), [trialId]);

  const { data: trials, error } = useSWR<Trial[]>(
    `/api/tasks/${taskId}/trials?probe=true`,
    fetcher,
    {
      refreshInterval: (data) => {
        const t = (data ?? []).find((x) => x.id === currentId);
        return t && (t.status === "success" || t.status === "failed")
          ? 0
          : 3000;
      },
    },
  );

  // ~30s grace before treating a not-yet-registered trial as truly missing —
  // covers the race where the probe row was just created.
  const [waitedTooLong, setWaitedTooLong] = useState(false);
  useEffect(() => {
    setWaitedTooLong(false);
    const t = setTimeout(() => setWaitedTooLong(true), 30_000);
    return () => clearTimeout(t);
  }, [currentId]);

  // Newest-first, matching the workbench history table — drives prev/next.
  const probes = [...(trials ?? [])].sort((a, b) => {
    const at = a.started_at ? new Date(a.started_at).getTime() : 0;
    const bt = b.started_at ? new Date(b.started_at).getTime() : 0;
    return bt - at;
  });
  const index = probes.findIndex((t) => t.id === currentId);
  const trial = index >= 0 ? probes[index] : undefined;
  const hasPrev = index > 0;
  const hasNext = index >= 0 && index < probes.length - 1;

  // Cloud trials don't inline `_artifacts`; pull them on demand once terminal.
  const needsArtifacts =
    !!trial &&
    !trial.result?._artifacts &&
    (trial.status === "success" || trial.status === "failed");
  const { data: fetchedArtifacts } = useSWR<Artifacts>(
    needsArtifacts ? `/api/trials/${currentId}/probe-artifacts` : null,
    fetcher,
  );

  let body: React.ReactNode;
  if (error) {
    body = (
      <p className="text-sm text-red-500">
        Failed to load trial: {error.message}
      </p>
    );
  } else if (!trials) {
    body = <p className="text-sm text-muted-foreground">Loading…</p>;
  } else if (!trial) {
    body = waitedTooLong ? (
      <p className="text-sm text-red-500">Trial not found.</p>
    ) : (
      <p className="text-sm text-muted-foreground">
        Waiting for the trial to register…
      </p>
    );
  } else {
    const extra = trial.harbor_config?.extra_instructions ?? "";
    const artifacts = trial.result?._artifacts ?? fetchedArtifacts;
    const messages = artifacts?.agent_messages ?? [];
    const verifierStdout = artifacts?.verifier_stdout;

    body = (
      <div className="space-y-6">
        <div className="flex items-center justify-between gap-2">
          <p className="text-xs uppercase tracking-wide text-muted-foreground">
            Probe run
          </p>
          {/* In contentOnly (standalone page) the URL drives which probe shows;
              in-place nav belongs to the drawer, so hide it there. */}
          {!contentOnly && probes.length > 1 ? (
            <div className="flex items-center gap-1">
              <button
                type="button"
                disabled={!hasPrev}
                onClick={() => hasPrev && setCurrentId(probes[index - 1].id)}
                className="rounded border p-1 text-muted-foreground enabled:hover:text-foreground disabled:opacity-30"
                title="Newer probe"
              >
                <ChevronLeft className="h-4 w-4" />
              </button>
              <span className="text-xs text-muted-foreground tabular-nums">
                {index + 1} / {probes.length}
              </span>
              <button
                type="button"
                disabled={!hasNext}
                onClick={() => hasNext && setCurrentId(probes[index + 1].id)}
                className="rounded border p-1 text-muted-foreground enabled:hover:text-foreground disabled:opacity-30"
                title="Older probe"
              >
                <ChevronRight className="h-4 w-4" />
              </button>
            </div>
          ) : null}
        </div>

        <div>
          <p className="text-sm">
            Task:{" "}
            <Link
              href={`/tasks/${taskId}`}
              className="font-mono font-medium text-blue-600 underline-offset-4 hover:text-blue-700 hover:underline"
            >
              {taskId}
            </Link>
          </p>
          <p className="mt-1 font-mono text-xs text-muted-foreground">
            {trial.id}
          </p>
          <p className="mt-2 text-sm">
            Preset:{" "}
            <span className="font-mono font-medium">
              {trial.harbor_config?.probe_name?.trim() || trial.agent}
            </span>
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

        <section className="rounded border p-4 space-y-3">
          <h2 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Agent process
          </h2>
          {messages.length === 0 ? (
            <p className="text-xs text-muted-foreground">(no messages yet)</p>
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

        <section className="rounded border p-4 space-y-2">
          <h2 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Operator instructions
          </h2>
          <pre className="whitespace-pre-wrap rounded bg-muted p-3 font-mono text-xs">
            {extra || "(none)"}
          </pre>
        </section>

        <section className="rounded border p-4 space-y-2">
          <h2 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Verifier output
          </h2>
          <pre className="overflow-auto whitespace-pre-wrap rounded bg-muted p-3 font-mono text-xs max-h-[400px]">
            {verifierStdout || "(empty)"}
          </pre>
        </section>

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

  if (contentOnly) return <>{body}</>;

  return (
    <ResizableDrawer open={isOpen} onOpenChange={(open) => !open && onClose()}>
      <div className="flex-1 overflow-auto p-6">{body}</div>
    </ResizableDrawer>
  );
}

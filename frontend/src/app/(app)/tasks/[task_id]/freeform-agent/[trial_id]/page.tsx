"use client";

import { use } from "react";
import useSWR from "swr";
import Link from "next/link";
import { useAuth } from "@clerk/nextjs";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8800";

// Mirrors the subset of TrialResponse we need on this page. The backend doesn't
// expose a GET /trials/{trial_id} that returns TrialResponse, so we list trials
// for the task and find the matching one — same data source the history table
// uses (see freeform-history-table.tsx).
type Trial = {
  id: string;
  agent: string;
  model: string | null;
  status: string;
  reward: number | null;
  started_at: string | null;
  finished_at: string | null;
  error_message: string | null;
  result: Record<string, unknown> | null;
  // harbor_config is not currently exposed on TrialResponse; the optional
  // chain below is defensive — once Task 15 surfaces this field the operator
  // instructions section will start populating.
  harbor_config?: { mode?: string; extra_instructions?: string } | null;
};

const TERMINAL_STATUSES = new Set(["success", "failed"]);

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
      // Template missing — fall back to default session token.
    }
    if (!token) {
      token = await getToken();
    }
    const res = await fetch(url, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!res.ok) {
      const body = await res.text();
      throw new Error(`HTTP ${res.status}: ${body.slice(0, 200)}`);
    }
    return res.json();
  };

  const { data: trials, error } = useSWR<Trial[]>(
    `${API_URL}/tasks/${task_id}/trials`,
    fetcher,
    {
      refreshInterval: (data) => {
        const trial = data?.find((t) => t.id === trial_id);
        if (trial && TERMINAL_STATUSES.has(trial.status)) return 0;
        return 3000;
      },
    },
  );

  if (error)
    return (
      <div className="container mx-auto max-w-3xl py-8">
        <p className="text-sm text-red-500">
          Failed to load trial: {error.message}
        </p>
        <Link
          href={`/tasks/${task_id}/freeform-agent`}
          className="text-xs underline"
        >
          ← Back to workbench
        </Link>
      </div>
    );

  if (!trials)
    return (
      <div className="container mx-auto max-w-3xl py-8">
        <p className="text-sm text-muted-foreground">Loading trial…</p>
      </div>
    );

  const trial = trials.find((t) => t.id === trial_id);

  if (!trial)
    return (
      <div className="container mx-auto max-w-3xl py-8 space-y-3">
        <p className="text-sm text-red-500">
          Trial <span className="font-mono">{trial_id}</span> not found for this
          task.
        </p>
        <Link
          href={`/tasks/${task_id}/freeform-agent`}
          className="text-xs underline"
        >
          ← Back to workbench
        </Link>
      </div>
    );

  const extraInstructions = trial.harbor_config?.extra_instructions ?? "";
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

      <section className="rounded border p-4 space-y-2">
        <h2 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Status
        </h2>
        <p className="text-sm">
          <span className="inline-block rounded bg-muted px-2 py-1 text-xs font-mono">
            {trial.status}
          </span>{" "}
          · agent: <code className="text-xs">{trial.agent}</code>
          {trial.model ? (
            <>
              {" "}
              · model: <code className="text-xs">{trial.model}</code>
            </>
          ) : null}
        </p>
        {trial.reward !== null && trial.reward !== undefined ? (
          <p className="text-sm">
            Reward: <strong>{trial.reward.toFixed(2)}</strong>{" "}
            {cheatFound ? (
              <span className="ml-2 inline-block rounded bg-red-500/15 px-2 py-1 text-xs font-medium text-red-600">
                Cheat found
              </span>
            ) : (
              <span className="ml-2 inline-block rounded bg-emerald-500/15 px-2 py-1 text-xs font-medium text-emerald-700">
                Clean
              </span>
            )}
          </p>
        ) : null}
        {trial.error_message ? (
          <p className="text-sm text-red-500 break-words whitespace-pre-wrap">
            {trial.error_message}
          </p>
        ) : null}
      </section>

      <section className="rounded border p-4 space-y-2">
        <h2 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Operator instructions
        </h2>
        <pre className="whitespace-pre-wrap rounded bg-muted p-3 font-mono text-xs">
          {extraInstructions || "(none)"}
        </pre>
      </section>

      <section className="rounded border p-4 space-y-2">
        <h2 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Trial result (raw)
        </h2>
        <pre className="overflow-auto rounded bg-muted p-3 font-mono text-xs max-h-[600px]">
          {JSON.stringify(trial.result ?? {}, null, 2)}
        </pre>
      </section>
    </div>
  );
}

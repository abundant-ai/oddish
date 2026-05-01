"use client";

import Link from "next/link";
import useSWR from "swr";
import { useAuth } from "@clerk/nextjs";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8800";

type Attempt = {
  success?: boolean | null;
};

type AnalyzerResult = {
  attempts?: Attempt[];
  cheating_attempted?: boolean | null;
  cheating_succeeded?: boolean | null;
  result_focus_findings?: string | null;
} | null;

// Row returned by GET /tasks/{task_id}/probe-runs.
type ProbeRun = {
  id: string;
  status: string;
  task_id: string;
  created_at: string | null;
  analyzer_result: AnalyzerResult;
};

type ResultDisplay = {
  text: string;
  variant: "cheat" | "blocked" | "neutral" | "muted" | "error";
  title?: string;
};

function statusLabel(r: ProbeRun): string {
  if (r.status === "queued" || r.status === "pending") return "queued";
  if (r.status === "running") return "running";
  if (r.status === "success" || r.status === "completed") return "done";
  if (r.status === "failed") return "failed";
  return r.status;
}

function resultDisplay(r: ProbeRun): ResultDisplay {
  const analysis = r.analyzer_result;

  if (r.status === "queued" || r.status === "pending") {
    return { text: "—", variant: "muted" };
  }
  if (r.status === "running") {
    return { text: "—", variant: "muted", title: "Run in progress" };
  }
  if (r.status === "failed") {
    return { text: "harness error", variant: "error" };
  }

  if (analysis === null || analysis === undefined) {
    return { text: "no result", variant: "muted", title: "Waiting for analyzer" };
  }

  const attempts = analysis.attempts ?? [];
  const succeeded = attempts.filter((a) => a.success === true).length;
  const blocked = attempts.filter((a) => a.success === false).length;
  const cheatAttempts = succeeded + blocked;

  if (cheatAttempts > 0) {
    const text =
      succeeded > 0
        ? `${succeeded} cheat${succeeded === 1 ? "" : "s"} succeeded`
        : `${blocked} blocked`;
    const tipParts: string[] = [];
    if (succeeded > 0) tipParts.push(`${succeeded} succeeded (verifier was bypassed)`);
    if (blocked > 0) tipParts.push(`${blocked} blocked by verifier`);
    return {
      text,
      variant: succeeded > 0 ? "cheat" : "blocked",
      title: tipParts.join(" · "),
    };
  }

  if (analysis.result_focus_findings) {
    const findings = analysis.result_focus_findings;
    const truncated =
      findings.length > 60 ? `${findings.slice(0, 60)}…` : findings;
    return { text: truncated, variant: "neutral", title: findings };
  }

  if (analysis.cheating_attempted === true) {
    return analysis.cheating_succeeded
      ? { text: "cheat succeeded", variant: "cheat" }
      : { text: "cheat blocked", variant: "blocked" };
  }
  if (analysis.cheating_attempted === false) {
    return {
      text: "no cheat attempted",
      variant: "neutral",
      title: "Agent did not attempt to cheat",
    };
  }

  return { text: "no result", variant: "muted" };
}

const VARIANT_CLASS: Record<ResultDisplay["variant"], string> = {
  cheat:
    "rounded bg-red-500/15 px-2 py-0.5 text-[11px] font-medium text-red-600",
  blocked:
    "rounded bg-emerald-500/15 px-2 py-0.5 text-[11px] font-medium text-emerald-700",
  neutral: "rounded bg-muted px-2 py-0.5 text-[11px] font-medium",
  muted: "text-[11px] text-muted-foreground",
  error:
    "rounded bg-amber-500/15 px-2 py-0.5 text-[11px] font-medium text-amber-700",
};

export function ProbeHistoryTable({ taskId }: { taskId: string }) {
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
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  };

  const { data, error, mutate } = useSWR<ProbeRun[]>(
    `${API_URL}/tasks/${taskId}/probe-runs`,
    fetcher,
    { refreshInterval: 5000 },
  );

  async function deleteRun(runId: string) {
    if (!confirm("Delete this probe run? This cannot be undone.")) return;
    let token: string | null = null;
    try {
      token = await getToken({ template: "oddish" });
    } catch {
      // fall through to default session token
    }
    if (!token) token = await getToken();
    const res = await fetch(
      `${API_URL}/tasks/${taskId}/probe-runs/${runId}`,
      {
        method: "DELETE",
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      },
    );
    if (!res.ok) {
      alert(`Delete failed: HTTP ${res.status}`);
      return;
    }
    mutate((cur) => (cur ?? []).filter((x) => x.id !== runId), false);
  }

  if (error)
    return (
      <p className="text-sm text-red-500">
        Failed to load history: {error.message}
      </p>
    );
  if (!data)
    return <p className="text-sm text-muted-foreground">Loading history…</p>;

  return (
    <div>
      <h2 className="mb-3 text-lg font-medium">History</h2>
      {data.length === 0 ? (
        <p className="text-sm text-muted-foreground">No probe runs yet.</p>
      ) : (
        <table className="w-full text-sm">
          <thead className="text-left text-muted-foreground">
            <tr>
              <th className="py-2 pr-4 font-medium">Timestamp</th>
              <th className="py-2 pr-4 font-medium">Status</th>
              <th className="py-2 pr-4 font-medium">Result</th>
              <th className="py-2 font-medium"></th>
            </tr>
          </thead>
          <tbody>
            {data.map((r) => (
              <tr key={r.id} className="border-t">
                <td className="py-2 pr-4 font-mono text-xs">
                  {r.created_at
                    ? new Date(r.created_at).toLocaleString()
                    : "—"}
                </td>
                <td className="py-2 pr-4">{statusLabel(r)}</td>
                <td className="py-2 pr-4">
                  {(() => {
                    const rd = resultDisplay(r);
                    return (
                      <span className={VARIANT_CLASS[rd.variant]} title={rd.title}>
                        {rd.text}
                      </span>
                    );
                  })()}
                </td>
                <td className="py-2">
                  <div className="flex items-center gap-3">
                    <button
                      type="button"
                      onClick={() => deleteRun(r.id)}
                      className="text-xs text-red-600 underline hover:text-red-700"
                    >
                      Del
                    </button>
                    <Link
                      href={`/tasks/${taskId}/probe/${r.id}`}
                      className="text-xs underline"
                    >
                      View →
                    </Link>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

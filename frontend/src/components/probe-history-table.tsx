"use client";

import Link from "next/link";
import useSWR from "swr";
import { useAuth } from "@clerk/nextjs";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8800";

type Attempt = {
  success?: boolean | null;
};

type EvaluationMetric = "cheat_ratio" | "result_focus" | "none";

type Analysis = {
  attempts?: Attempt[];
  cheating_attempted?: boolean | null;
  cheating_succeeded?: boolean | null;
  result_focus_findings?: string | null;
};

type Trial = {
  id: string;
  agent: string;
  status: string;
  started_at: string | null;
  reward: number | null;
  error_message?: string | null;
  analysis?: Analysis | null;
  harbor_config?: { mode?: string; evaluation_metric?: EvaluationMetric } | null;
};

function statusLabel(t: Trial): string {
  if (t.status === "queued" || t.status === "pending") return "queued";
  if (t.status === "running") return "running";
  if (t.status === "success") return "done";
  if (t.status === "failed") return "failed";
  return t.status;
}

type ResultDisplay = {
  text: string;
  variant: "cheat" | "blocked" | "neutral" | "muted" | "error";
  title?: string;
};

function resultDisplay(t: Trial): ResultDisplay {
  // Branch on metric first so a probe-agent type renders the SAME column
  // shape regardless of trial state. Within each metric branch, status
  // determines the placeholder vs. real content.
  const metric = t.harbor_config?.evaluation_metric ?? "none";

  if (metric === "cheat_ratio") {
    if (t.status === "queued" || t.status === "pending") {
      return { text: "—/— cheats", variant: "muted", title: "Trial queued" };
    }
    if (t.status === "running") {
      return { text: "—/— cheats", variant: "muted", title: "Trial running" };
    }
    if (t.status === "failed") {
      return {
        text: "harness error",
        variant: "error",
        title: t.error_message ?? "Trial failed before producing a result",
      };
    }
    // status === "success"
    if (t.analysis === null || t.analysis === undefined) {
      return {
        text: "—/— cheats",
        variant: "muted",
        title: "Waiting for analyzer",
      };
    }
    const all = t.analysis.attempts ?? [];
    const succeeded = all.filter((a) => a.success === true).length;
    const blocked = all.filter((a) => a.success === false).length;
    const total = succeeded + blocked;
    if (total === 0) {
      return {
        text: "0/0 cheats",
        variant: "neutral",
        title:
          "Analyzer ran but found no cheat attempts in the transcript",
      };
    }
    return {
      text: `${succeeded}/${total} cheats succeeded`,
      variant: succeeded > 0 ? "cheat" : "blocked",
      title:
        succeeded > 0
          ? `${succeeded} of ${total} cheat attempts bypassed the verifier — task is gameable`
          : `All ${total} cheat attempts were blocked by the verifier — task is robust`,
    };
  }

  if (metric === "result_focus") {
    if (t.status === "queued" || t.status === "pending") {
      return {
        text: "awaiting answer",
        variant: "muted",
        title: "Trial queued",
      };
    }
    if (t.status === "running") {
      return {
        text: "awaiting answer",
        variant: "muted",
        title: "Trial running",
      };
    }
    if (t.status === "failed") {
      return {
        text: "harness error",
        variant: "error",
        title: t.error_message ?? "Trial failed before producing a result",
      };
    }
    // status === "success"
    if (t.analysis === null || t.analysis === undefined) {
      return {
        text: "awaiting answer",
        variant: "muted",
        title: "Waiting for analyzer",
      };
    }
    const findings = t.analysis.result_focus_findings;
    if (!findings) {
      return {
        text: "awaiting answer",
        variant: "muted",
        title: "Analyzer ran but produced no answer",
      };
    }
    const truncated =
      findings.length > 60 ? `${findings.slice(0, 60)}…` : findings;
    return {
      text: truncated,
      variant: "neutral",
      title: findings,
    };
  }

  // metric === "none" — fallback path (no format-locking).

  // In-flight or pending — nothing to report yet
  if (t.status === "queued" || t.status === "pending") {
    return { text: "—", variant: "muted" };
  }
  if (t.status === "running") {
    return { text: "—", variant: "muted", title: "Trial in progress" };
  }
  // Harness-level failure (agent didn't get to run, or backend orphaned)
  if (t.status === "failed") {
    return {
      text: "harness error",
      variant: "error",
      title: t.error_message ?? "Trial failed before producing a result",
    };
  }

  // Prefer the analyzer's per-attempt cheat ratio.
  // Counts: succeeded=success===true, blocked=success===false,
  // investigation=success===null/undefined (not a cheat attempt at all).
  const attempts = t.analysis?.attempts ?? [];
  const succeeded = attempts.filter((a) => a.success === true).length;
  const blocked = attempts.filter((a) => a.success === false).length;
  const investigation = attempts.length - succeeded - blocked;
  const cheatAttempts = succeeded + blocked;

  if (cheatAttempts > 0) {
    // Concise primary text + verbose tooltip. Color is operator-centric:
    // any cheat that bypassed the verifier = red (task is gameable). All
    // blocked = green (task is robust).
    const text =
      succeeded > 0
        ? `${succeeded} cheat${succeeded === 1 ? "" : "s"} succeeded`
        : `${blocked} blocked`;
    const tipParts: string[] = [];
    if (succeeded > 0) tipParts.push(`${succeeded} succeeded (verifier was bypassed)`);
    if (blocked > 0) tipParts.push(`${blocked} blocked by verifier`);
    if (investigation > 0)
      tipParts.push(`${investigation} investigation step${investigation === 1 ? "" : "s"} (not cheat attempts)`);
    return {
      text,
      variant: succeeded > 0 ? "cheat" : "blocked",
      title: tipParts.join(" · "),
    };
  }

  // No structured attempt data. Fall back to top-level cheat verdict.
  if (t.analysis?.cheating_attempted === true) {
    return t.analysis.cheating_succeeded
      ? { text: "cheat succeeded", variant: "cheat" }
      : { text: "cheat blocked", variant: "blocked" };
  }
  if (t.analysis?.cheating_attempted === false) {
    return {
      text: "no cheat attempted",
      variant: "neutral",
      title:
        "Agent did not attempt to cheat (may have done legitimate work or just analyzed)",
    };
  }

  // Analyzer hasn't filled the trial yet, just show raw reward
  if (t.reward === null || t.reward === undefined) {
    return { text: "no result", variant: "muted" };
  }
  return {
    text: `reward ${t.reward.toFixed(2)}`,
    variant: "neutral",
    title: "Analyzer hasn't classified this run yet — raw verifier reward",
  };
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

  const { data, error } = useSWR<Trial[]>(
    `${API_URL}/tasks/${taskId}/trials`,
    fetcher,
    { refreshInterval: 5000 },
  );

  if (error)
    return (
      <p className="text-sm text-red-500">
        Failed to load history: {error.message}
      </p>
    );
  if (!data)
    return <p className="text-sm text-muted-foreground">Loading history…</p>;

  // If harbor_config is exposed (Task 15+), narrow to probe runs only.
  // Otherwise show every trial for the task — better than hiding the whole
  // history if the field hasn't been wired through yet.
  const anyHaveHarborConfig = data.some(
    (t) => t.harbor_config !== undefined && t.harbor_config !== null,
  );
  const probes = anyHaveHarborConfig
    ? data.filter((t) => t.harbor_config?.mode === "probe")
    : data;

  return (
    <div>
      <h2 className="mb-3 text-lg font-medium">History</h2>
      {probes.length === 0 ? (
        <p className="text-sm text-muted-foreground">No probe runs yet.</p>
      ) : (
        <table className="w-full text-sm">
          <thead className="text-left text-muted-foreground">
            <tr>
              <th className="py-2 pr-4 font-medium">Timestamp</th>
              <th className="py-2 pr-4 font-medium">Agent</th>
              <th className="py-2 pr-4 font-medium">Status</th>
              <th className="py-2 pr-4 font-medium">Result</th>
              <th className="py-2 font-medium"></th>
            </tr>
          </thead>
          <tbody>
            {probes.map((t) => (
              <tr key={t.id} className="border-t">
                <td className="py-2 pr-4 font-mono text-xs">
                  {t.started_at ? new Date(t.started_at).toLocaleString() : "—"}
                </td>
                <td className="py-2 pr-4">{t.agent}</td>
                <td className="py-2 pr-4">{statusLabel(t)}</td>
                <td className="py-2 pr-4">
                  {(() => {
                    const r = resultDisplay(t);
                    return (
                      <span className={VARIANT_CLASS[r.variant]} title={r.title}>
                        {r.text}
                      </span>
                    );
                  })()}
                </td>
                <td className="py-2">
                  <Link
                    href={`/tasks/${taskId}/probe/${t.id}`}
                    className="text-xs underline"
                  >
                    View →
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

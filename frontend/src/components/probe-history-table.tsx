"use client";

import Link from "next/link";
import useSWR from "swr";

type Attempt = {
  success?: boolean | null;
};

// "cheat_ratio"/"ratio" are legacy aliases — normalizeMetric maps them to "none".
type EvaluationMetric = "result_focus" | "none" | "cheat_ratio" | "ratio";

type Analysis = {
  attempts?: Attempt[];
  cheating_attempted?: boolean | null;
  cheating_succeeded?: boolean | null;
  result_focus_findings?: string | Record<string, unknown> | unknown[] | null;
};

type Trial = {
  id: string;
  agent: string;
  status: string;
  started_at: string | null;
  reward: number | null;
  error_message?: string | null;
  analysis?: Analysis | null;
  harbor_config?: {
    mode?: string;
    evaluation_metric?: EvaluationMetric;
    probe_name?: string | null;
  } | null;
  is_probe?: boolean;
};

// What to show in the run-identity column for a probe.
// Newer probe runs carry the operator-selected preset name in
// harbor_config.probe_name. Older runs (and runs launched without a
// preset) won't — decide what to fall back to.
function probeLabel(t: Trial): string {
  // Prefer the operator-selected preset name; fall back to the model so
  // older runs (and preset-less runs) still show something meaningful.
  return t.harbor_config?.probe_name?.trim() || t.agent;
}

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
  const rawMetric = t.harbor_config?.evaluation_metric ?? "none";
  // Legacy "cheat_ratio"/"ratio" fall back to "none".
  const metric =
    rawMetric === "result_focus" ? "result_focus" : ("none" as const);

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
    if (typeof findings === "object") {
      if (Array.isArray(findings)) {
        const len = findings.length;
        return {
          text: `${len} item${len === 1 ? "" : "s"}`,
          variant: "neutral",
          title: "structured findings — open run for full detail",
        };
      }
      const keyCount = Object.keys(findings as Record<string, unknown>).length;
      return {
        text: `${keyCount} field${keyCount === 1 ? "" : "s"}`,
        variant: "neutral",
        title: "structured findings — open run for full detail",
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
    if (succeeded > 0)
      tipParts.push(`${succeeded} succeeded (verifier was bypassed)`);
    if (blocked > 0) tipParts.push(`${blocked} blocked by verifier`);
    if (investigation > 0)
      tipParts.push(
        `${investigation} investigation step${investigation === 1 ? "" : "s"} (not cheat attempts)`,
      );
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
  // Fetch through the same-origin Next proxy (/api/...) so the browser never
  // hits the backend cross-origin — no CORS, auth attached server-side.
  const fetcher = async (url: string) => {
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  };

  const { data, error } = useSWR<Trial[]>(
    `/api/tasks/${taskId}/trials?probe=true`,
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

  // The server already filtered to probes (?probe=true); show newest first.
  const probes = [...data].sort((a, b) => {
    const at = a.started_at ? new Date(a.started_at).getTime() : 0;
    const bt = b.started_at ? new Date(b.started_at).getTime() : 0;
    return bt - at;
  });

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
              <th className="py-2 pr-4 font-medium">Probe</th>
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
                <td className="py-2 pr-4">{probeLabel(t)}</td>
                <td className="py-2 pr-4">{statusLabel(t)}</td>
                <td className="py-2 pr-4">
                  {(() => {
                    const r = resultDisplay(t);
                    return (
                      <span
                        className={VARIANT_CLASS[r.variant]}
                        title={r.title}
                      >
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

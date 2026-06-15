"use client";

import Link from "next/link";
import useSWR from "swr";
import { ExternalLink, Microscope } from "lucide-react";
import {
  isTerminalProbeStatus,
  normalizeMetric,
  pluralize,
  PRIORITY_META,
  ratioUnitVerb,
  sortRecommendations,
  tallyAttempts,
  type ProbeTrial,
} from "@/lib/probe-summary";

// Concise, color-coded result for the latest-probe card. Mirrors the wording
// in probe-history-table's result column but derives from the shared
// probe-summary helpers so the two stay consistent.
type ResultDisplay = {
  text: string;
  variant: "cheat" | "blocked" | "neutral" | "muted" | "error";
  title?: string;
};

function probeLabel(t: ProbeTrial): string {
  // Prefer the operator-selected preset name; fall back to the agent/model so
  // older (preset-less) runs still show something meaningful.
  return t.harbor_config?.probe_name?.trim() || t.agent;
}

function statusLabel(t: ProbeTrial): string {
  if (t.status === "queued" || t.status === "pending") return "queued";
  if (t.status === "running") return "running";
  if (t.status === "success") return "done";
  if (t.status === "failed") return "failed";
  return t.status;
}

function resultDisplay(t: ProbeTrial): ResultDisplay {
  const metric = normalizeMetric(t.harbor_config?.evaluation_metric);
  const { unit, verb } = ratioUnitVerb(t.harbor_config ?? null);

  if (!isTerminalProbeStatus(t.status)) {
    return { text: "—", variant: "muted", title: `Trial ${statusLabel(t)}` };
  }
  if (t.status === "failed") {
    return {
      text: "harness error",
      variant: "error",
      title: t.error_message ?? "Trial failed before producing a result",
    };
  }
  // status === "success" — needs the analyzer to have run.
  if (!t.analysis) {
    return { text: "awaiting analyzer", variant: "muted" };
  }

  if (metric === "result_focus") {
    const findings = t.analysis.result_focus_findings;
    if (!findings) return { text: "awaiting answer", variant: "muted" };
    const truncated =
      findings.length > 80 ? `${findings.slice(0, 80)}…` : findings;
    return { text: truncated, variant: "neutral", title: findings };
  }

  // ratio + none both report on cheat attempts.
  const plural = pluralize(unit);
  const verbStr = verb ? ` ${verb}` : "";
  const { succeeded, cheatTotal } = tallyAttempts(t.analysis.attempts);

  if (cheatTotal === 0) {
    // Fall back to the top-level cheat verdict when there are no per-attempt rows.
    if (t.analysis.cheating_attempted === true) {
      return t.analysis.cheating_succeeded
        ? { text: "cheat succeeded", variant: "cheat" }
        : { text: "cheat blocked", variant: "blocked" };
    }
    return {
      text: `0/0 ${plural}`,
      variant: "neutral",
      title: `Analyzer found no ${plural} in the transcript`,
    };
  }
  return {
    text: `${succeeded}/${cheatTotal} ${plural}${verbStr}`,
    variant: succeeded > 0 ? "cheat" : "blocked",
    title:
      succeeded > 0
        ? `${succeeded} of ${cheatTotal} ${plural}${verbStr} — task is gameable`
        : `All ${cheatTotal} ${plural} were blocked — task is robust`,
  };
}

const VARIANT_CLASS: Record<ResultDisplay["variant"], string> = {
  cheat:
    "rounded bg-red-500/15 px-2 py-0.5 text-[11px] font-medium text-red-600",
  blocked:
    "rounded bg-emerald-500/15 px-2 py-0.5 text-[11px] font-medium text-emerald-700",
  neutral:
    "rounded bg-[color:var(--paper-line-2)] px-2 py-0.5 text-[11px] font-medium text-[color:var(--paper-ink-2)]",
  muted: "text-[11px] text-[color:var(--paper-ink-3)]",
  error:
    "rounded bg-amber-500/15 px-2 py-0.5 text-[11px] font-medium text-amber-700",
};

const fetcher = async (url: string) => {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
};

export function TaskProbeRunCard({
  taskId,
  versionId,
}: {
  taskId: string;
  versionId: string | null;
}) {
  const { data } = useSWR<ProbeTrial[]>(
    `/api/tasks/${taskId}/trials?probe=true`,
    fetcher,
    {
      refreshInterval: (rows) => {
        // Poll while the newest probe run for this version is still in flight.
        const latest = pickLatest(rows ?? [], versionId);
        return latest && !isTerminalProbeStatus(latest.status) ? 5000 : 0;
      },
    },
  );

  const latest = pickLatest(data, versionId);

  return (
    <div className="space-y-3">
      <div className="flex items-baseline justify-between">
        <h2 className="font-mono text-[12px] font-semibold tracking-[0.06em] text-[color:var(--paper-ink-2)] uppercase">
          Probe runs
        </h2>
        <Link
          href={`/tasks/${taskId}/probe`}
          className="font-mono text-[10.5px] text-[color:var(--paper-ink-3)] underline hover:text-[color:var(--paper-ink)]"
        >
          View all →
        </Link>
      </div>

      {!latest ? (
        <div className="rounded-[10px] border border-dashed border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-4 py-6 text-center text-[12px] text-[color:var(--paper-ink-3)]">
          No probe run for this version yet.
        </div>
      ) : (
        <ProbeRow taskId={taskId} trial={latest} />
      )}
    </div>
  );
}

function ProbeRow({ taskId, trial }: { taskId: string; trial: ProbeTrial }) {
  const result = resultDisplay(trial);
  const hasRecsField = Array.isArray(trial.analysis?.recommendations);
  const recs = hasRecsField
    ? sortRecommendations(trial.analysis?.recommendations)
    : [];
  return (
    <div className="space-y-3 rounded-[10px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-4 py-3">
      {/* Identity row */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          <Microscope
            className="h-4 w-4 shrink-0 text-[color:var(--paper-ink-3)]"
            aria-hidden="true"
          />
          <span className="truncate font-mono text-[13px] font-semibold text-[color:var(--paper-ink)]">
            {probeLabel(trial)}
          </span>
          <span className="font-mono text-[11px] text-[color:var(--paper-ink-3)]">
            {statusLabel(trial)}
          </span>
          <span className={VARIANT_CLASS[result.variant]} title={result.title}>
            {result.text}
          </span>
        </div>
        <div className="flex shrink-0 items-center gap-3">
          {trial.created_at ? (
            <span
              className="font-mono text-[11px] text-[color:var(--paper-ink-3)]"
              title={new Date(trial.created_at).toLocaleString()}
            >
              {new Date(trial.created_at).toLocaleString()}
            </span>
          ) : null}
          <Link
            href={`/tasks/${taskId}/probe/${trial.id}`}
            className="inline-flex items-center gap-1 font-mono text-[11px] text-[color:var(--paper-ink-2)] underline hover:text-[color:var(--paper-ink)]"
          >
            View run
            <ExternalLink className="h-3 w-3" aria-hidden="true" />
          </Link>
        </div>
      </div>

      {/* Action items from the latest probe's analysis */}
      {hasRecsField ? (
        <div className="border-t border-[color:var(--paper-line-2)] pt-3">
          <p className="mb-2 font-mono text-[10px] font-semibold tracking-[0.09em] text-[color:var(--paper-ink-3)] uppercase">
            Action items
          </p>
          {recs.length > 0 ? (
            <ul className="space-y-1.5">
              {recs.map((r, i) => {
                const meta =
                  PRIORITY_META[r.priority] ?? PRIORITY_META.should_fix;
                return (
                  <li
                    key={i}
                    className="flex items-start gap-2 text-[12px] leading-snug"
                  >
                    <span
                      className={`mt-0.5 shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold tracking-wide uppercase ${meta.cls}`}
                    >
                      {meta.label}
                    </span>
                    <span className="text-[color:var(--paper-ink)]">
                      <span className="font-medium">{r.action}</span>
                      {r.rationale ? (
                        <span className="text-[color:var(--paper-ink-3)]">
                          {" "}
                          — {r.rationale}
                        </span>
                      ) : null}
                    </span>
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="text-[12px] text-emerald-700">
              No fixes needed — task held up to probing.
            </p>
          )}
        </div>
      ) : null}
    </div>
  );
}

// Backend returns probe trials created_at ASC; pick the newest one whose
// task_version_id matches the selected version tab.
function pickLatest(
  rows: ProbeTrial[] | undefined,
  versionId: string | null,
): ProbeTrial | null {
  if (!rows || rows.length === 0) return null;
  const scoped = versionId
    ? rows.filter((t) => t.task_version_id === versionId)
    : rows;
  return scoped.length > 0 ? scoped[scoped.length - 1] : null;
}

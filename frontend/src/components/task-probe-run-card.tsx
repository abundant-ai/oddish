"use client";

import type { ReactNode } from "react";
import Link from "next/link";
import useSWR from "swr";
import { ExternalLink, Microscope } from "lucide-react";
import {
  isTerminalProbeStatus,
  normalizeMetric,
  PRIORITY_META,
  sortRecommendations,
  type ProbeTrial,
} from "@/lib/probe-summary";

// Concise, color-coded result for the latest-probe card. Mirrors the wording
// in probe-history-table's result column but derives from the shared
// probe-summary helpers so the two stay consistent.
type ResultDisplay = {
  text: string;
  variant: "attention" | "clean" | "neutral" | "muted" | "error";
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
      findings.length > 80 ? `${findings.slice(0, 80)}…` : findings;
    return { text: truncated, variant: "neutral", title: findings };
  }

  // metric === "none" — summarize by the action items the probe surfaced.
  const recs = sortRecommendations(t.analysis.recommendations);
  const mustFix = recs.filter((r) => r.priority === "must_fix").length;
  if (recs.length === 0) {
    return {
      text: "no action items",
      variant: "clean",
      title: "Task held up to probing — no fixes recommended",
    };
  }
  const count = `${recs.length} action item${recs.length === 1 ? "" : "s"}`;
  return {
    text: mustFix > 0 ? `${count} · ${mustFix} must-fix` : count,
    variant: mustFix > 0 ? "attention" : "neutral",
    title:
      mustFix > 0
        ? `${recs.length} recommended fixes, ${mustFix} must-fix`
        : `${recs.length} recommended fixes`,
  };
}

const VARIANT_CLASS: Record<ResultDisplay["variant"], string> = {
  attention:
    "rounded bg-red-500/15 px-2 py-0.5 text-[11px] font-medium text-red-600",
  clean:
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
  headerSlot,
}: {
  taskId: string;
  versionId: string | null;
  headerSlot?: ReactNode;
}) {
  const { data } = useSWR<ProbeTrial[]>(
    `/api/tasks/${taskId}/trials?probe=true`,
    fetcher,
    {
      refreshInterval: (rows) => {
        // Poll while any per-type latest run for this version is still in flight.
        const latest = pickLatestByType(rows ?? [], versionId);
        return latest.some((t) => !isTerminalProbeStatus(t.status)) ? 5000 : 0;
      },
    },
  );

  const latest = pickLatestByType(data, versionId);

  return (
    <div className="space-y-3">
      <div className="flex items-baseline justify-between">
        <h2 className="font-mono text-[12px] font-semibold tracking-[0.06em] text-[color:var(--paper-ink-2)] uppercase">
          Task Analysis
        </h2>
        <Link
          href={`/tasks/${taskId}/probe`}
          className="font-mono text-[10.5px] text-[color:var(--paper-ink-3)] underline hover:text-[color:var(--paper-ink)]"
        >
          View all →
        </Link>
      </div>

      {headerSlot}

      {latest.length === 0 ? (
        <div className="rounded-[10px] border border-dashed border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-4 py-6 text-center text-[12px] text-[color:var(--paper-ink-3)]">
          No probe run for this version yet.
        </div>
      ) : (
        latest.map((trial) => (
          <ProbeSection key={trial.id} taskId={taskId} trial={trial} />
        ))
      )}
    </div>
  );
}

// One delineated section per probe type. The header labels the probe type and
// the agent that produced it; the existing ProbeRow body renders inside.
function ProbeSection({
  taskId,
  trial,
}: {
  taskId: string;
  trial: ProbeTrial;
}) {
  const title = probeLabel(trial);
  const agentLabel = [trial.agent, trial.model].filter(Boolean).join(" · ");
  // probeLabel() falls back to the agent name, so for preset-less runs the
  // title already IS the agent — skip the redundant sub-line then.
  const showAgent = agentLabel && agentLabel !== title;
  return (
    <section className="space-y-2 rounded-[10px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h3 className="font-mono text-[12px] font-semibold tracking-[0.04em] text-[color:var(--paper-ink)]">
          {title}
        </h3>
        {showAgent ? (
          <span className="font-mono text-[10.5px] text-[color:var(--paper-ink-3)]">
            agent: {agentLabel}
          </span>
        ) : null}
      </div>
      <ProbeRow taskId={taskId} trial={trial} />
    </section>
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

function probeTypeKey(t: ProbeTrial): string {
  // Same key as probeLabel(): prefer the preset name, fall back to the agent.
  return t.harbor_config?.probe_name?.trim() || t.agent;
}

// Backend returns probe trials created_at ASC. For the selected version, keep
// the newest run of each distinct probe type, freshest type first.
function pickLatestByType(
  rows: ProbeTrial[] | undefined,
  versionId: string | null,
): ProbeTrial[] {
  if (!rows || rows.length === 0) return [];
  const scoped = versionId
    ? rows.filter((t) => t.task_version_id === versionId)
    : rows;
  const latestByType = new Map<string, ProbeTrial>();
  for (const t of scoped) latestByType.set(probeTypeKey(t), t); // ASC → last wins
  return [...latestByType.values()].sort(
    (a, b) => Date.parse(b.created_at ?? "") - Date.parse(a.created_at ?? ""),
  );
}

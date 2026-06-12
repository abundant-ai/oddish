"use client";

import type { ReactNode } from "react";
import {
  type ProbeTrial,
  normalizeMetric,
  ratioUnitVerb,
  tallyAttempts,
  pluralize,
} from "@/lib/probe-summary";

const PRIORITY_ORDER: Record<string, number> = {
  must_fix: 0,
  should_fix: 1,
  optional: 2,
};

const PRIORITY_META: Record<string, { label: string; cls: string }> = {
  must_fix: { label: "Must fix", cls: "bg-red-500/15 text-red-600" },
  should_fix: { label: "Should fix", cls: "bg-amber-500/15 text-amber-700" },
  optional: { label: "Optional", cls: "bg-slate-500/15 text-slate-600" },
};

// The single probe-summary rendering, shared by the probe-run detail page and
// the task-drawer probe card so both show exactly the same summary. `action`
// renders on the right of the header (e.g. the drawer's "View full probe run"
// link); the detail page leaves it empty.
export function ProbeRunSummary({
  trial,
  action,
}: {
  trial: ProbeTrial;
  action?: ReactNode;
}) {
  const summary = trial.analysis;
  const analysisFailed =
    trial.analysis_status === "FAILED" || trial.analysis_status === "failed";
  const pending =
    trial.status === "running" ||
    trial.status === "queued" ||
    trial.status === "pending";

  if (!summary) {
    if (analysisFailed) {
      return (
        <section className="rounded border p-4">
          <p className="text-xs text-red-500">
            Summary failed: {trial.analysis_error ?? "(no detail)"}
          </p>
        </section>
      );
    }
    if (pending) {
      return (
        <section className="rounded border p-4">
          <p className="text-xs text-muted-foreground">
            Summary will appear once the trial completes.
          </p>
        </section>
      );
    }
    return null;
  }

  const metric = normalizeMetric(trial.harbor_config?.evaluation_metric);
  const { unit, verb } = ratioUnitVerb(trial.harbor_config);
  const plural = pluralize(unit);
  const verbStr = verb ? ` ${verb}` : "";
  const { succeeded, blocked, investigation, cheatTotal } = tallyAttempts(
    summary.attempts,
  );
  const hasRecsField = Array.isArray(summary.recommendations);
  const recs = hasRecsField
    ? [...(summary.recommendations ?? [])].sort(
        (a, b) =>
          (PRIORITY_ORDER[a.priority] ?? 9) - (PRIORITY_ORDER[b.priority] ?? 9),
      )
    : [];
  const mustFixCount = recs.filter((r) => r.priority === "must_fix").length;
  const metricLabel =
    metric === "ratio"
      ? "ratio metric"
      : metric === "result_focus"
        ? "result focus metric"
        : "no specific metric";

  return (
    <section className="space-y-3 rounded border-2 border-primary/30 bg-primary/5 p-4">
      <div className="flex items-center justify-between">
        <h2 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Summary
        </h2>
        <div className="flex items-center gap-2">
          <span className="rounded bg-muted px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
            {metricLabel}
          </span>
          <span className="text-[10px] text-muted-foreground">
            {summary.model ?? ""}
          </span>
          {action}
        </div>
      </div>

      {/* Always render the result_focus block when metric is result_focus,
          even if findings are missing — show an "awaiting answer" placeholder.
          Legacy rows (no metric set) still render it when both fields exist. */}
      {metric === "result_focus" ? (
        <div className="mb-2 space-y-2 rounded border-2 border-amber-500/30 bg-amber-500/5 p-3">
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
            <p className="text-sm italic text-muted-foreground">
              awaiting answer
            </p>
          )}
        </div>
      ) : summary.result_focus_question && summary.result_focus_findings ? (
        <div className="mb-2 space-y-2 rounded border-2 border-amber-500/30 bg-amber-500/5 p-3">
          <p className="text-xs font-medium uppercase tracking-wide text-amber-700">
            Result focus
          </p>
          <p className="text-sm font-medium italic">
            {summary.result_focus_question}
          </p>
          <p className="text-sm">{summary.result_focus_findings}</p>
        </div>
      ) : null}

      {summary.headline ? (
        <p className="text-base font-medium leading-snug">{summary.headline}</p>
      ) : null}
      {summary.summary ? (
        <p className="text-sm leading-relaxed">{summary.summary}</p>
      ) : null}

      {hasRecsField ? (
        <div className="rounded border bg-muted/20 p-3">
          <div className="mb-2 flex items-center gap-2">
            <p className="text-xs font-semibold uppercase tracking-wide text-foreground">
              Action items
            </p>
            {mustFixCount > 0 ? (
              <span className="rounded bg-red-500/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-red-600">
                {mustFixCount} must-fix
              </span>
            ) : null}
          </div>
          {recs.length > 0 ? (
            <ul className="space-y-2">
              {recs.map((r, i) => {
                const meta = PRIORITY_META[r.priority] ?? PRIORITY_META.should_fix;
                return (
                  <li key={i} className="flex items-start gap-2 text-sm">
                    <span
                      className={`mt-0.5 shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${meta.cls}`}
                    >
                      {meta.label}
                    </span>
                    <span className="leading-snug">
                      <span className="font-medium">{r.action}</span>
                      {r.rationale ? (
                        <span className="text-muted-foreground">
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
            <p className="text-sm text-emerald-700">
              No fixes needed — task held up to probing.
            </p>
          )}
        </div>
      ) : null}

      {summary.key_actions && summary.key_actions.length > 0 ? (
        <div>
          <p className="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Key actions
          </p>
          <ul className="list-disc space-y-1 pl-5 text-sm">
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

      {/* Ratio probes render the breakdown chips even when attempts is empty so
          the verdict shape stays consistent; other metrics only when classified
          attempts exist. */}
      {metric === "ratio" ? (
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
              {succeeded > 0 ? "task is gameable" : "task is robust"}
            </span>
          ) : (
            <span className="italic text-muted-foreground">
              no {plural} identified
            </span>
          )}
        </div>
      ) : cheatTotal > 0 || investigation > 0 ? (
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
              {succeeded > 0 ? "task is gameable" : "task is robust"}
            </span>
          ) : null}
        </div>
      ) : null}

      {summary.evidence ? (
        <p className="text-xs italic text-muted-foreground">
          Evidence: {summary.evidence}
        </p>
      ) : null}

      {summary.tool_insights && summary.tool_insights.length > 0 ? (
        <div>
          <p className="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Tools &amp; skills used
          </p>
          <ul className="space-y-1 text-sm">
            {summary.tool_insights.map((t, i) => (
              <li key={i} className="flex items-start gap-2">
                <span className="mt-0.5 shrink-0 rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium uppercase text-muted-foreground">
                  {t.kind === "mcp" ? "MCP" : "Skill"}
                </span>
                <span>
                  {t.name ? (
                    <span className="font-mono text-xs">{t.name}</span>
                  ) : null}
                  {t.name && t.note ? " — " : null}
                  {t.note}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}

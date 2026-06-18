"use client";

import type { ReactNode } from "react";
import {
  type ProbeTrial,
  normalizeMetric,
  PRIORITY_META,
  sortRecommendations,
} from "@/lib/probe-summary";

// Pretty-print structured findings; for a string, parse JSON when possible so
// it renders as formatted code, falling back to the verbatim text otherwise.
function formatFindings(findings: unknown): string {
  if (typeof findings === "object" && findings !== null) {
    return JSON.stringify(findings, null, 2);
  }
  const text = String(findings);
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

// The result-focus block: a plain-language summary of the findings up top, with
// the structured JSON tucked behind a toggle (always formatted code, never a
// <p>). Falls back to "awaiting answer" until the findings arrive.
function ResultFocus({
  summary,
  findings,
}: {
  summary?: string | null;
  findings: unknown;
}) {
  return (
    <div className="space-y-2 rounded border-2 border-amber-500/30 bg-amber-500/5 p-3">
      <p className="text-xs font-medium uppercase tracking-wide text-amber-700">
        Result focus
      </p>
      {findings == null ? (
        <p className="text-sm italic text-muted-foreground">awaiting answer</p>
      ) : (
        <>
          {summary ? (
            <p className="text-sm leading-relaxed">{summary}</p>
          ) : null}
          <details className="group mt-1">
            <summary className="cursor-pointer select-none text-xs font-medium text-amber-700 hover:underline">
              Show structured JSON output
            </summary>
            <pre className="mt-2 overflow-auto rounded bg-muted/50 p-2 text-xs font-mono whitespace-pre">
              {formatFindings(findings)}
            </pre>
          </details>
        </>
      )}
    </div>
  );
}

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
  const hasRecsField = Array.isArray(summary.recommendations);
  const recs = hasRecsField ? sortRecommendations(summary.recommendations) : [];
  const mustFixCount = recs.filter((r) => r.priority === "must_fix").length;
  const metricLabel =
    metric === "result_focus" ? "result focus metric" : "no specific metric";

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

      {/* Action items first, then the result-focus recap (summary of the JSON
          with the structured output behind a toggle), then the whole-session
          summary. */}
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
                const meta =
                  PRIORITY_META[r.priority] ?? PRIORITY_META.should_fix;
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

      {/* Show the result-focus block when the metric asks for it (even before
          findings arrive) or whenever a legacy row carries a summary/findings. */}
      {metric === "result_focus" ||
      summary.result_focus_summary ||
      summary.result_focus_findings != null ? (
        <ResultFocus
          summary={summary.result_focus_summary}
          findings={summary.result_focus_findings}
        />
      ) : null}

      {summary.headline ? (
        <p className="text-base font-medium leading-snug">{summary.headline}</p>
      ) : null}
      {summary.summary ? (
        <p className="text-sm leading-relaxed">{summary.summary}</p>
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

"use client";

import Link from "next/link";
import { ExternalLink } from "lucide-react";
import {
  type ProbeTrial,
  normalizeMetric,
  ratioUnitVerb,
  tallyAttempts,
  pluralize,
} from "@/lib/probe-summary";

export function TaskProbeSummary({
  trial,
  taskId,
}: {
  trial: ProbeTrial;
  taskId: string;
}) {
  const summary = trial.analysis;
  const metric = normalizeMetric(trial.harbor_config?.evaluation_metric);
  const { unit, verb } = ratioUnitVerb(trial.harbor_config);
  const plural = pluralize(unit);
  const verbStr = verb ? ` ${verb}` : "";
  const tally = tallyAttempts(summary?.attempts);
  const hasReward = trial.reward !== null && trial.reward !== undefined;
  const cheatFound = hasReward && (trial.reward as number) >= 0.5;

  const pending =
    trial.status === "running" ||
    trial.status === "queued" ||
    trial.status === "pending";
  const analysisFailed =
    trial.analysis_status === "FAILED" || trial.analysis_status === "failed";

  return (
    <div className="space-y-4 p-4 sm:p-6">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">
          Probe
        </h2>
        <Link
          href={`/tasks/${taskId}/probe/${trial.id}`}
          className="text-muted-foreground hover:text-foreground inline-flex items-center gap-1 text-xs underline"
        >
          View full probe run
          <ExternalLink className="h-3 w-3" aria-hidden="true" />
        </Link>
      </div>

      {/* Status row */}
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="bg-muted rounded px-2 py-1 font-mono">
          {trial.status}
        </span>
        <span className="text-muted-foreground">
          {trial.agent}
          {trial.model ? ` · ${trial.model}` : ""}
        </span>
        {hasReward ? (
          <span className="text-muted-foreground">
            reward <strong>{(trial.reward as number).toFixed(2)}</strong>
          </span>
        ) : null}
        {hasReward ? (
          cheatFound ? (
            <span className="rounded bg-red-500/15 px-2 py-1 font-medium text-red-600">
              Cheat may have succeeded
            </span>
          ) : (
            <span className="rounded bg-emerald-500/15 px-2 py-1 font-medium text-emerald-700">
              Verifier failed (reward &lt; 0.5)
            </span>
          )
        ) : null}
      </div>

      {summary ? (
        <div className="space-y-3">
          {/* result_focus block */}
          {metric === "result_focus" ? (
            <div className="space-y-1 rounded border-2 border-amber-500/30 bg-amber-500/5 p-3">
              <p className="text-xs font-medium tracking-wide text-amber-700 uppercase">
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
                <p className="text-muted-foreground text-sm italic">
                  awaiting answer
                </p>
              )}
            </div>
          ) : null}

          {summary.headline ? (
            <p className="text-base leading-snug font-medium">
              {summary.headline}
            </p>
          ) : null}
          {summary.summary ? (
            <p className="text-sm leading-relaxed">{summary.summary}</p>
          ) : null}

          {/* Metric chips (ratio + result_focus/none share the breakdown) */}
          {metric === "ratio" ? (
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span
                className={`rounded px-2 py-1 font-medium ${
                  tally.succeeded > 0
                    ? "bg-red-500/15 text-red-600"
                    : "bg-muted text-muted-foreground"
                }`}
              >
                {tally.succeeded} {tally.succeeded === 1 ? unit : plural}
                {verbStr}
              </span>
              <span
                className={`rounded px-2 py-1 font-medium ${
                  tally.blocked > 0
                    ? "bg-emerald-500/15 text-emerald-700"
                    : "bg-muted text-muted-foreground"
                }`}
              >
                {tally.blocked} blocked
              </span>
              {tally.investigation > 0 ? (
                <span className="bg-muted text-muted-foreground rounded px-2 py-1 font-medium">
                  {tally.investigation} investigation step
                  {tally.investigation === 1 ? "" : "s"}
                </span>
              ) : null}
              <span className="text-muted-foreground">
                {tally.cheatTotal > 0
                  ? tally.succeeded > 0
                    ? "task is gameable"
                    : "task is robust"
                  : `no ${plural} identified`}
              </span>
            </div>
          ) : tally.cheatTotal > 0 || tally.investigation > 0 ? (
            <div className="flex flex-wrap items-center gap-2 text-xs">
              {tally.succeeded > 0 ? (
                <span className="rounded bg-red-500/15 px-2 py-1 font-medium text-red-600">
                  {tally.succeeded} {tally.succeeded === 1 ? unit : plural}
                  {verbStr}
                </span>
              ) : null}
              {tally.blocked > 0 ? (
                <span className="rounded bg-emerald-500/15 px-2 py-1 font-medium text-emerald-700">
                  {tally.blocked} blocked
                </span>
              ) : null}
              {tally.investigation > 0 ? (
                <span className="bg-muted text-muted-foreground rounded px-2 py-1 font-medium">
                  {tally.investigation} investigation step
                  {tally.investigation === 1 ? "" : "s"}
                </span>
              ) : null}
              {tally.cheatTotal > 0 ? (
                <span className="text-muted-foreground">
                  {tally.succeeded > 0 ? "task is gameable" : "task is robust"}
                </span>
              ) : null}
            </div>
          ) : null}

          {summary.evidence ? (
            <p className="text-muted-foreground text-xs italic">
              Evidence: {summary.evidence}
            </p>
          ) : null}
        </div>
      ) : analysisFailed ? (
        <p className="text-xs text-red-500">
          Summary failed: {trial.analysis_error ?? "(no detail)"}
        </p>
      ) : pending ? (
        <p className="text-muted-foreground text-xs">
          Summary will appear once the probe completes.
        </p>
      ) : (
        <p className="text-muted-foreground text-xs">No summary available.</p>
      )}
    </div>
  );
}

"use client";

import {
  ArrowUpRight,
  CheckCircle2,
  CircleX,
  FlaskConical,
  Loader2,
  SearchCode,
  TriangleAlert,
} from "lucide-react";

import { AnalysisProse } from "@/components/analysis-prose";
import { SeverityGroups } from "@/components/qa-report/action-items";
import { CopyJsonButton } from "@/components/qa-report/copy-json-button";
import { FALLBACK_TOKEN, VERDICT_TOKENS } from "@/components/qa-report/tokens";
import { TaskVerdictBadge } from "@/components/task-verdict-badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { formatCostUsd, formatDurationSec } from "@/lib/format";
import type {
  AnalysisClassification,
  ReviewFinding,
  TaskReviewBaselineResult,
  TaskReviewResponse,
  TaskReviewTrial,
} from "@/lib/types";
import { cn } from "@/lib/utils";

const CLASSIFICATION_ORDER: AnalysisClassification[] = [
  "BAD_SUCCESS",
  "BAD_FAILURE",
  "HARNESS_ERROR",
  "GOOD_FAILURE",
  "GOOD_SUCCESS",
];

const CLASSIFICATION_LABELS: Record<AnalysisClassification, string> = {
  BAD_SUCCESS: "Bad success",
  BAD_FAILURE: "Bad failure",
  HARNESS_ERROR: "Harness error",
  GOOD_FAILURE: "Good failure",
  GOOD_SUCCESS: "Good success",
};

function trialLabel(trial: TaskReviewTrial): string {
  const model = trial.model?.split("/").pop();
  return model ? `${trial.agent} · ${model}` : trial.agent;
}

function baselineLabel(
  name: "nop" | "oracle",
  baseline: TaskReviewBaselineResult
): string {
  const expected = baseline.expected_reward === 1 ? "pass" : "fail";
  if (baseline.trial_count === 0) return `No ${name} baseline`;
  if (baseline.valid) return `Valid · expected ${expected}`;
  return `Faulty · ${baseline.unexpected_count} unexpected`;
}

function verifierLabel(trial: TaskReviewTrial): {
  label: string;
  className: string;
} {
  if (trial.status === "failed") {
    return { label: "Harness failure", className: "text-orange-600" };
  }
  if (trial.status !== "success") {
    return { label: trial.status, className: "text-muted-foreground" };
  }
  if (trial.reward === 1) {
    return { label: "Pass · 1", className: "text-emerald-600" };
  }
  if (trial.reward === 0) {
    return { label: "Fail · 0", className: "text-amber-600" };
  }
  if (trial.reward != null) {
    return {
      label: `Partial · ${trial.reward.toFixed(3)}`,
      className: "text-amber-600",
    };
  }
  return { label: "No reward", className: "text-muted-foreground" };
}

function BaselineCard({
  name,
  baseline,
}: {
  name: "nop" | "oracle";
  baseline: TaskReviewBaselineResult;
}) {
  const valid = baseline.valid && baseline.trial_count > 0;
  const Icon = valid ? CheckCircle2 : CircleX;
  return (
    <div
      className={cn(
        "rounded-lg border p-3",
        valid
          ? "border-emerald-500/30 bg-emerald-500/5"
          : "border-amber-500/30 bg-amber-500/5"
      )}
    >
      <div className="flex items-center gap-2">
        <Icon
          className={cn(
            "h-4 w-4",
            valid ? "text-emerald-500" : "text-amber-500"
          )}
        />
        <span className="font-mono text-xs font-semibold uppercase">
          {name}
        </span>
      </div>
      <p className="text-muted-foreground mt-1 font-mono text-[11px]">
        {baselineLabel(name, baseline)}
      </p>
      <p className="text-muted-foreground mt-0.5 font-mono text-[10px]">
        {baseline.trial_count} trial{baseline.trial_count === 1 ? "" : "s"}
      </p>
    </div>
  );
}

function TrialQa({ trial }: { trial: TaskReviewTrial }) {
  const classification = trial.analysis?.classification;
  if (!classification) {
    const active =
      trial.analysis_status === "pending" ||
      trial.analysis_status === "queued" ||
      trial.analysis_status === "running" ||
      trial.analysis_status === "retrying";
    return (
      <span className="text-muted-foreground inline-flex items-center gap-1 font-mono text-[11px]">
        {active ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
        {active ? "Analyzing" : "Not analyzed"}
      </span>
    );
  }
  const token = VERDICT_TOKENS[classification] ?? FALLBACK_TOKEN;
  const Icon = token.icon;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[10px] font-semibold",
        token.chip,
        token.accent
      )}
    >
      <Icon className="h-3 w-3" />
      {CLASSIFICATION_LABELS[classification]}
    </span>
  );
}

function TrialRow({
  trial,
  onOpen,
}: {
  trial: TaskReviewTrial;
  onOpen: (trialId: string) => void;
}) {
  const verifier = verifierLabel(trial);
  return (
    <li className="border-border border-b px-3 py-2.5 last:border-b-0">
      <div className="grid grid-cols-[minmax(0,1.5fr)_minmax(7rem,0.8fr)_minmax(8rem,1fr)] gap-3">
        <div className="min-w-0">
          <button
            type="button"
            onClick={() => onOpen(trial.id)}
            className="text-foreground flex max-w-full items-center gap-1 font-mono text-[11px] font-semibold hover:text-blue-500"
          >
            <span className="truncate">{trialLabel(trial)}</span>
            <ArrowUpRight className="h-3 w-3 shrink-0" />
          </button>
          <div className="text-muted-foreground mt-0.5 flex flex-wrap gap-x-2 font-mono text-[9.5px]">
            <span>{trial.role}</span>
            {trial.environment ? <span>{trial.environment}</span> : null}
            {trial.duration_seconds != null ? (
              <span>{formatDurationSec(trial.duration_seconds)}</span>
            ) : null}
            {trial.cost_usd != null ? (
              <span>{formatCostUsd(trial.cost_usd)}</span>
            ) : null}
          </div>
        </div>
        <span className={cn("font-mono text-[11px]", verifier.className)}>
          {verifier.label}
        </span>
        <div className="min-w-0">
          <TrialQa trial={trial} />
          {trial.included_in_result_run ? (
            <p className="text-muted-foreground mt-1 font-mono text-[9px]">
              Included in published QA
            </p>
          ) : null}
          {trial.analysis_matches_result_run === false ? (
            <p className="mt-1 font-mono text-[9px] text-amber-600">
              Analysis changed after QA
            </p>
          ) : null}
        </div>
      </div>
      {trial.analysis ? (
        <details className="mt-2">
          <summary className="text-muted-foreground hover:text-foreground cursor-pointer font-mono text-[10px]">
            QA evidence
          </summary>
          <div className="border-border mt-2 border-l pl-3">
            <div className="flex justify-end">
              <CopyJsonButton
                value={trial.analysis}
                label={`trial QA: ${trialLabel(trial)}`}
                compact
              />
            </div>
            <p className="text-muted-foreground font-mono text-[10px]">
              {trial.analysis.subtype}
            </p>
            {trial.analysis.evidence ? (
              <AnalysisProse text={trial.analysis.evidence} className="mt-1" />
            ) : null}
            {trial.analysis.root_cause ? (
              <AnalysisProse
                text={trial.analysis.root_cause}
                className="mt-1"
              />
            ) : null}
          </div>
        </details>
      ) : null}
    </li>
  );
}

export function TaskOverviewPanel({
  review,
  loading,
  loadError,
  onRerunChecks,
  checksRerunning,
  checksQueueError,
  onShowMoreFindings,
  findingsLoadingMore,
  findingsPageError,
  onShowMoreTrials,
  trialsLoadingMore,
  trialsPageError,
  onOpenTrial,
  className,
}: {
  review?: TaskReviewResponse;
  loading?: boolean;
  loadError?: string | null;
  onRerunChecks: () => void;
  checksRerunning: boolean;
  checksQueueError?: string | null;
  onShowMoreFindings: () => void;
  findingsLoadingMore: boolean;
  findingsPageError?: string | null;
  onShowMoreTrials: () => void;
  trialsLoadingMore: boolean;
  trialsPageError?: string | null;
  onOpenTrial?: (trialId: string) => boolean;
  className?: string;
}) {
  if (loading) {
    return (
      <div className={cn("space-y-3 p-4", className)}>
        <Skeleton className="h-16 w-full" />
        <div className="grid grid-cols-2 gap-3">
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
        </div>
        <Skeleton className="h-40 w-full" />
      </div>
    );
  }

  if (loadError || !review) {
    return (
      <div
        className={cn("flex h-full items-center justify-center p-6", className)}
      >
        <div className="max-w-md text-center">
          <TriangleAlert className="mx-auto h-5 w-5 text-amber-500" />
          <p className="text-muted-foreground mt-2 text-sm">
            {loadError ?? "Task review is unavailable."}
          </p>
        </div>
      </div>
    );
  }

  const qaActive =
    review.qa.active_run != null ||
    review.qa.status === "pending" ||
    review.qa.status === "queued" ||
    review.qa.status === "running" ||
    review.qa.status === "retrying";
  const openTrial = (trialId: string) => {
    if (onOpenTrial?.(trialId)) return;
    const params = new URLSearchParams({
      version: review.task.version_id,
      trial: trialId,
    });
    window.location.assign(`/tasks/${review.task.id}?${params.toString()}`);
  };
  const trialById = new Map(review.trials.map((trial) => [trial.id, trial]));
  const renderFindingSource = (finding: ReviewFinding) => (
    <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
      <span className="text-muted-foreground font-mono text-[9.5px]">
        Source:
      </span>
      {finding.from_pre_trial ? (
        <span className="border-border rounded border px-1.5 py-0.5 font-mono text-[9.5px]">
          source audit
        </span>
      ) : null}
      {finding.trial_ids.map((trialId) => (
        <button
          type="button"
          key={trialId}
          onClick={() => openTrial(trialId)}
          className="border-border inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[9.5px] hover:border-blue-500 hover:text-blue-500"
        >
          {trialById.has(trialId)
            ? trialLabel(trialById.get(trialId)!)
            : `trial ${trialId.slice(0, 8)}`}
          <ArrowUpRight className="h-2.5 w-2.5" />
        </button>
      ))}
      {finding.experiment_ids.map((experimentId) => (
        <a
          key={experimentId}
          href={`/experiments/${experimentId}`}
          className="text-muted-foreground font-mono text-[9.5px] hover:text-blue-500"
        >
          experiment {experimentId.slice(0, 8)}
        </a>
      ))}
    </div>
  );

  return (
    <div className={cn("space-y-4 p-4", className)}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <SearchCode className="h-4 w-4 text-blue-500" />
            <h2 className="font-mono text-sm font-semibold">Task QA review</h2>
            <span className="border-border text-muted-foreground rounded border px-1.5 py-0.5 font-mono text-[10px]">
              v{review.task.version}
            </span>
          </div>
          <p className="text-muted-foreground mt-1 font-mono text-[10.5px]">
            {review.scope.experiment_id
              ? `Experiment ${review.scope.experiment_id}`
              : "All experiments on this version"}
          </p>
        </div>
        <CopyJsonButton value={review} label="task QA review" compact />
      </div>

      <TaskVerdictBadge
        verdictStatus={review.qa.status}
        verdict={review.verdict}
        runAnalysis
        qaInFlight={qaActive}
        selectedVersionId={review.task.version_id}
        verdictVersionId={review.qa.result_run?.task_version_id}
        publishedQaRunId={review.qa.result_run?.id}
        variant="inline"
      />

      {review.qa.legacy_unscoped_verdict_available ? (
        <div className="rounded-md border border-amber-500/40 bg-amber-500/5 px-3 py-2 font-mono text-[11px] text-amber-700 dark:text-amber-300">
          A legacy unscoped verdict exists. This view shows only version-owned
          QA evidence.
        </div>
      ) : null}
      {review.qa.input_analysis_changed_after_run ? (
        <div className="rounded-md border border-amber-500/40 bg-amber-500/5 px-3 py-2 font-mono text-[11px] text-amber-700 dark:text-amber-300">
          Trial analysis changed after the published QA run. Rerun QA before
          relying on this verdict.
        </div>
      ) : null}

      <section>
        <div className="mb-2 flex items-center gap-2">
          <FlaskConical className="h-3.5 w-3.5" />
          <h3 className="font-mono text-xs font-semibold uppercase">
            Baselines
          </h3>
          <span
            className={cn(
              "rounded px-1.5 py-0.5 font-mono text-[9px] font-semibold uppercase",
              review.baselines.outcome === "valid"
                ? "bg-emerald-500/10 text-emerald-600"
                : "bg-amber-500/10 text-amber-600"
            )}
          >
            {review.baselines.outcome}
          </span>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <BaselineCard name="nop" baseline={review.baselines.nop} />
          <BaselineCard name="oracle" baseline={review.baselines.oracle} />
        </div>
      </section>

      <section>
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <div>
            <h3 className="font-mono text-xs font-semibold uppercase">
              Findings
            </h3>
            <p className="text-muted-foreground mt-0.5 font-mono text-[10px]">
              {review.finding_counts.filtered_total} in scope ·{" "}
              {review.finding_counts.must_fix} must fix ·{" "}
              {review.finding_counts.should_fix} should fix ·{" "}
              {review.finding_counts.optional} optional
            </p>
          </div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={onRerunChecks}
            disabled={checksRerunning || qaActive}
            className="h-7 font-mono text-[10px]"
          >
            {checksRerunning ? (
              <Loader2 className="mr-1 h-3 w-3 animate-spin" />
            ) : null}
            Re-run source audit
          </Button>
        </div>
        {checksQueueError ? (
          <p className="mb-2 font-mono text-[11px] text-red-500">
            {checksQueueError}
          </p>
        ) : null}
        {review.findings.length ? (
          <SeverityGroups
            items={review.findings}
            renderItemFooter={renderFindingSource}
          />
        ) : (
          <div className="border-border text-muted-foreground rounded-lg border border-dashed p-4 text-center font-mono text-[11px]">
            No canonical findings for this scope.
          </div>
        )}
        {review.findings_page.has_more ? (
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={onShowMoreFindings}
            disabled={findingsLoadingMore}
            className="mt-2 h-7 w-full font-mono text-[10px]"
          >
            {findingsLoadingMore ? (
              <Loader2 className="mr-1 h-3 w-3 animate-spin" />
            ) : null}
            Show more findings
          </Button>
        ) : null}
        {findingsPageError ? (
          <p className="mt-1 font-mono text-[10px] text-red-500">
            {findingsPageError}
          </p>
        ) : null}
      </section>

      <section>
        <div className="mb-2">
          <h3 className="font-mono text-xs font-semibold uppercase">
            Trial QA
          </h3>
          <p className="text-muted-foreground mt-0.5 font-mono text-[10px]">
            {review.trial_counts.analyzed}/{review.trial_counts.eligible}{" "}
            analyzed
            {review.trial_counts.unanalyzed
              ? ` · ${review.trial_counts.unanalyzed} pending`
              : ""}
          </p>
          <div className="mt-1 flex flex-wrap gap-1.5">
            {CLASSIFICATION_ORDER.map((classification) => {
              const count = review.trial_counts.classifications[classification];
              if (!count) return null;
              return (
                <span
                  key={classification}
                  className="border-border rounded border px-1.5 py-0.5 font-mono text-[9.5px]"
                >
                  {CLASSIFICATION_LABELS[classification]} {count}
                </span>
              );
            })}
          </div>
        </div>
        <div className="border-border overflow-hidden rounded-lg border">
          <div className="bg-muted/40 text-muted-foreground grid grid-cols-[minmax(0,1.5fr)_minmax(7rem,0.8fr)_minmax(8rem,1fr)] gap-3 border-b px-3 py-1.5 font-mono text-[9px] font-semibold uppercase">
            <span>Trial</span>
            <span>Verifier</span>
            <span>QA classification</span>
          </div>
          <ul>
            {review.trials.map((trial) => (
              <TrialRow key={trial.id} trial={trial} onOpen={openTrial} />
            ))}
          </ul>
          {!review.trials.length ? (
            <p className="text-muted-foreground p-4 text-center font-mono text-[11px]">
              No eligible trials in this scope.
            </p>
          ) : null}
        </div>
        {review.trials_page.has_more ? (
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={onShowMoreTrials}
            disabled={trialsLoadingMore}
            className="mt-2 h-7 w-full font-mono text-[10px]"
          >
            {trialsLoadingMore ? (
              <Loader2 className="mr-1 h-3 w-3 animate-spin" />
            ) : null}
            Show more trials
          </Button>
        ) : null}
        {trialsPageError ? (
          <p className="mt-1 font-mono text-[10px] text-red-500">
            {trialsPageError}
          </p>
        ) : null}
      </section>
    </div>
  );
}

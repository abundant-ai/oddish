import type {
  AnalysisClassification,
  PreTrialFinding,
  Task,
  Trial,
} from "@/lib/types";
import { formatLineRange } from "@/lib/line-range";
import { taskVerdictProgress, taskAnalysisProgress } from "@/lib/job-status";
import { isBaselineAgentName } from "@/lib/experiment-agent-grouping";

export const EXECUTION_LABELS: Record<AnalysisClassification, string> = {
  GOOD_SUCCESS: "Good success",
  GOOD_FAILURE: "Good failure",
  BAD_SUCCESS: "Bad success",
  BAD_FAILURE: "Bad failure",
  HARNESS_ERROR: "Harness error",
};

export const VERDICT_LABELS = {
  accepted: "Accepted",
  needs_fixes: "Rejected",
  outdated: "No verdict",
  queued: "Verdict queued",
  running: "Verdict running",
  error: "No verdict",
  never: "No verdict",
};

/** Exclude internal, superseded, and baseline runs from review coverage. */
export function isReviewableTrial(trial: Trial): boolean {
  return (
    !trial.is_probe &&
    (trial.kind ?? "agent") === "agent" &&
    !trial.superseded_by_trial_id &&
    !isBaselineAgentName(trial.agent)
  );
}

/** Analysis completion is independent of whether the run produced a valid grade. */
export function runReviewCounts(trials: Trial[]) {
  const counts = {
    total: 0,
    analyzed: 0,
    failed: 0,
    running: 0,
    queued: 0,
  };
  for (const trial of trials) {
    if (!isReviewableTrial(trial)) continue;
    counts.total++;
    if (trial.analysis_status === "running") counts.running++;
    else if (
      trial.analysis_status === "pending" ||
      trial.analysis_status === "queued"
    )
      counts.queued++;
    else if (trial.analysis_status === "failed") counts.failed++;
    else if (
      trial.analysis_status === "success" &&
      trial.analysis?.classification
    )
      counts.analyzed++;
  }
  return counts;
}

export function runReviewSummary(trials: Trial[]): string {
  const counts = runReviewCounts(trials);
  if (!counts.total) return "No runs";
  return [
    `${counts.analyzed}/${counts.total} analyzed`,
    counts.failed
      ? `${counts.failed} ${counts.failed === 1 ? "analysis" : "analyses"} failed`
      : null,
    counts.running ? `${counts.running} analyzing` : null,
    counts.queued ? `${counts.queued} queued` : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

/** Published labels take precedence over legacy booleans; unknown is not rejection. */
export function verdictOutcome(
  verdict: { verdict?: string; is_good?: boolean | null } | null | undefined
): "accepted" | "needs_fixes" | null {
  const label = verdict?.verdict;
  if (label === "accept") return "accepted";
  if (label === "reject") return "needs_fixes";
  if (verdict?.is_good === true) return "accepted";
  if (verdict?.is_good === false) return "needs_fixes";
  return null;
}

/** Review progress and task quality; solver failure never determines this. */
export function taskReviewStatus(task: Task): keyof typeof VERDICT_LABELS {
  const verdict = verdictOutcome(task.verdict);
  const progress = taskVerdictProgress(task);
  if (progress) return progress;
  if (task.verdict_status === "failed") return "error";
  if (verdict == null) {
    const analysisProgress = taskAnalysisProgress(task);
    if (analysisProgress) return analysisProgress;
  }
  if (task.verdict && task.review_version_matches === false) return "outdated";
  if (verdict) return verdict;
  return "never";
}

/** Both task actions target the default version, even while viewing an older one. */
export function taskVerdictActionLabel(task: Task | null | undefined): string {
  const action =
    task?.verdict || task?.verdict_status
      ? "Regenerate verdict"
      : "Generate verdict";
  return `${action}${task?.current_version != null ? ` for v${task.current_version}` : ""}`;
}

export const VERDICT_FILTER_LABELS = {
  accepted: VERDICT_LABELS.accepted,
  rejected: VERDICT_LABELS.needs_fixes,
  queued: VERDICT_LABELS.queued,
  running: VERDICT_LABELS.running,
  no_verdict: VERDICT_LABELS.never,
};

export type TaskReviewFilter = "all" | keyof typeof VERDICT_FILTER_LABELS;

/** The disjoint groups shared by verdict counts, filters and drawer navigation. */
export function taskReviewFilter(task: Task): Exclude<TaskReviewFilter, "all"> {
  const status = taskReviewStatus(task);
  if (status === "accepted") return "accepted";
  if (status === "needs_fixes") return "rejected";
  if (status === "queued" || status === "running") return status;
  return "no_verdict";
}

/** A finding address pins content even when that version is today's default. */
export function findingHref(
  taskId: string,
  version: number,
  finding: PreTrialFinding,
  file = false
): string {
  const params = new URLSearchParams({
    version: String(version),
    drawer: "task",
  });
  if (finding.id) params.set("finding", finding.id);
  params.set("taskPane", file && finding.file ? "file" : "overview");
  if (finding.file) params.set("taskFile", finding.file);
  if (finding.line_start) {
    params.set(
      "taskLines",
      formatLineRange({
        start: finding.line_start,
        end: finding.line_end ?? finding.line_start,
      })
    );
  }
  return `/tasks/${encodeURIComponent(taskId)}?${params}`;
}

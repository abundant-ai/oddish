import type {
  AnalysisClassification,
  PreTrialFinding,
  Task,
  Trial,
} from "@/lib/types";
import { formatLineRange } from "@/lib/line-range";
import { taskHasActiveVerdict, taskHasActiveAnalysis } from "@/lib/job-status";
import { isBaselineAgentName } from "@/lib/experiment-agent-grouping";

export const EXECUTION_LABELS: Record<AnalysisClassification, string> = {
  GOOD_SUCCESS: "Agent succeeded",
  GOOD_FAILURE: "Good failure",
  BAD_SUCCESS: "Invalid success",
  BAD_FAILURE: "Task-caused failure",
  HARNESS_ERROR: "Could not evaluate run",
};

import { QA_STATUS_LABELS } from "@/lib/deliveries";

// Task reviews use outdated only for a version mismatch; deliveries also use it
// for evidence outside the selected time window or missing required evidence.
export const REVIEW_LABELS = {
  ...QA_STATUS_LABELS,
  outdated: "No result for this version",
};

export const VERDICT_LABELS = {
  accepted: "Accepted",
  needs_fixes: "Rejected",
  outdated: "No result for this version",
  queued: "Review queued",
  running: "Review running",
  error: "Review couldn’t finish",
  never: "Not reviewed",
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

/** A completed review may still be unable to evaluate a run. */
export function runReviewCounts(trials: Trial[]) {
  const counts = {
    total: 0,
    evaluated: 0,
    issues: 0,
    incomplete: 0,
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
    else if (trial.analysis_status === "failed") counts.incomplete++;
    else if (trial.analysis_status === "success") {
      switch (trial.analysis?.classification) {
        case "GOOD_SUCCESS":
        case "GOOD_FAILURE":
          counts.evaluated++;
          break;
        case "BAD_SUCCESS":
        case "BAD_FAILURE":
          counts.evaluated++;
          counts.issues++;
          break;
        case "HARNESS_ERROR":
          counts.incomplete++;
      }
    }
  }
  return counts;
}

export function runReviewSummary(trials: Trial[]): string {
  const counts = runReviewCounts(trials);
  if (!counts.total) return "No runs";
  return [
    `${counts.evaluated}/${counts.total} evaluated`,
    counts.issues ? `${counts.issues} with task issues` : null,
    counts.incomplete ? `${counts.incomplete} couldn’t be evaluated` : null,
    counts.running ? `${counts.running} reviewing` : null,
    counts.queued ? `${counts.queued} queued` : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

/** Review progress and task quality; solver failure never determines this. */
export function taskReviewStatus(task: Task): keyof typeof REVIEW_LABELS {
  const verdict =
    task.verdict?.verdict ??
    (task.verdict?.is_good === true
      ? "accept"
      : task.verdict?.is_good === false
        ? "reject"
        : null);
  if (
    taskHasActiveVerdict(task) ||
    (task.verdict_status !== "failed" &&
      verdict == null &&
      taskHasActiveAnalysis(task))
  ) {
    return task.verdict_status === "queued" || task.verdict_status === "pending"
      ? "queued"
      : "running";
  }
  if (task.verdict_status === "failed") return "error";
  if (task.verdict && task.review_version_matches === false) return "outdated";
  if (verdict === "reject") return "needs_fixes";
  if (verdict === "accept") return "accepted";
  return "never";
}

export type TaskReviewFilter =
  | "all"
  | "accepted"
  | "rejected"
  | "running"
  | "failed"
  | "unreviewed";

/** The disjoint groups shared by review counts, table filters and drawer navigation. */
export function taskReviewFilter(task: Task): Exclude<TaskReviewFilter, "all"> {
  const status = taskReviewStatus(task);
  if (status === "accepted") return "accepted";
  if (status === "needs_fixes") return "rejected";
  if (status === "error") return "failed";
  if (status === "queued" || status === "running") return "running";
  return "unreviewed";
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

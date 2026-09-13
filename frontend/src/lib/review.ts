import type {
  AnalysisClassification,
  PreTrialFinding,
  Task,
} from "@/lib/types";
import { formatLineRange } from "@/lib/line-range";
import { taskHasActiveVerdict, taskHasActiveAnalysis } from "@/lib/job-status";
import { isBaselineAgentName } from "@/lib/experiment-agent-grouping";

export const EXECUTION_LABELS: Record<AnalysisClassification, string> = {
  GOOD_SUCCESS: "Agent succeeded",
  GOOD_FAILURE: "Fair agent failure",
  BAD_SUCCESS: "Invalid success",
  BAD_FAILURE: "Task-caused failure",
  HARNESS_ERROR: "Execution not evaluated",
};

import { QA_STATUS_LABELS } from "@/lib/deliveries";

// Task reviews use outdated only for a version mismatch; deliveries also use it
// for evidence outside the selected time window or missing required evidence.
export const REVIEW_LABELS = {
  ...QA_STATUS_LABELS,
  outdated: "Review outdated",
};

export const VERDICT_LABELS = {
  accepted: "Accepted",
  needs_fixes: "Rejected",
  outdated: "Verdict outdated",
  queued: "Verdict queued",
  running: "Verdict running",
  error: "Verdict could not complete",
  never: "No verdict",
};

/** Source-audit state belongs to the selected experiment version, not its verdict. */
export function preTrialReviewLabel(task: Task): string {
  switch (task.pre_trial_status) {
    case "pending":
      return "Pending";
    case "queued":
      return "Queued";
    case "running":
      return "Running";
    case "failed":
      return "Could not complete";
    case "success":
      return task.must_fix_count == null
        ? "Completed"
        : task.must_fix_count > 0
          ? "Findings"
          : "Passed";
    default:
      return "Not reviewed";
  }
}

/** Count loaded solver reviews independently of solver reward and task verdict. */
export function postTrialReviewLabel(task: Task): string {
  const counts = {
    passed: 0,
    failed: 0,
    incomplete: 0,
    running: 0,
    pending: 0,
    unreviewed: 0,
  };
  // Explicit null denotes the experiment's legacy, versionless trial rows.
  const versionId =
    task.trial_version_id !== undefined
      ? task.trial_version_id
      : task.current_version_id;
  for (const trial of task.trials ?? []) {
    if (
      trial.is_probe ||
      (trial.kind ?? "agent") !== "agent" ||
      trial.superseded_by_trial_id ||
      isBaselineAgentName(trial.agent) ||
      (versionId !== undefined && (trial.task_version_id ?? null) !== versionId)
    )
      continue;
    if (trial.analysis_status === "running") {
      counts.running++;
    } else if (
      trial.analysis_status === "pending" ||
      trial.analysis_status === "queued"
    ) {
      counts.pending++;
    } else if (trial.analysis_status === "failed") {
      counts.incomplete++;
    } else if (trial.analysis_status === "success") {
      switch (trial.analysis?.classification) {
        case "GOOD_SUCCESS":
        case "GOOD_FAILURE":
          counts.passed++;
          break;
        case "BAD_SUCCESS":
        case "BAD_FAILURE":
          counts.failed++;
          break;
        case "HARNESS_ERROR":
          counts.incomplete++;
          break;
        default:
          counts.unreviewed++;
      }
    } else {
      counts.unreviewed++;
    }
  }
  const labels = {
    passed: "passed",
    failed: "failed",
    incomplete: "could not complete",
    running: "running",
    pending: "pending",
    unreviewed: "unreviewed",
  };
  return (
    Object.entries(counts)
      .filter(([, count]) => count > 0)
      .map(([key, count]) => `${count} ${labels[key as keyof typeof labels]}`)
      .join(" · ") || "No solver reviews loaded"
  );
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

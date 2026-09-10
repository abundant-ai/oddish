import type {
  AnalysisClassification,
  PreTrialFinding,
  Task,
} from "@/lib/types";
import { formatLineRange } from "@/lib/line-range";
import { taskHasActiveVerdict, taskHasActiveAnalysis } from "@/lib/job-status";

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

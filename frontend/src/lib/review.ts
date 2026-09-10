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

export { QA_STATUS_LABELS as REVIEW_LABELS } from "@/lib/deliveries";
import { QA_STATUS_LABELS } from "@/lib/deliveries";

/** Review progress and task quality; solver failure never determines this. */
export function taskReviewStatus(task: Task): keyof typeof QA_STATUS_LABELS {
  if (
    taskHasActiveVerdict(task) ||
    (task.verdict_status !== "failed" &&
      task.verdict?.is_good == null &&
      taskHasActiveAnalysis(task))
  ) {
    return task.verdict_status === "queued" || task.verdict_status === "pending"
      ? "queued"
      : "running";
  }
  if (task.verdict_status === "failed") return "error";
  if (task.verdict && task.review_version_matches === false) return "outdated";
  if (task.verdict?.is_good === false) return "needs_fixes";
  if (task.verdict?.is_good === true) return "accepted";
  return "never";
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

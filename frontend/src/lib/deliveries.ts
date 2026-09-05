import type {
  DeliveryBoardResponse,
  DeliveryCheckStatus,
  DeliveryQAStatus,
  DeliveryTaskBoardRow,
  QAIssueCategory,
} from "@/lib/types";

export const QA_ISSUE_LABELS: Record<QAIssueCategory, string> = {
  instructions: "Instructions",
  verifier: "Verifier / grading",
  environment: "Environment / runtime",
  evidence: "Missing evidence",
  qa_execution: "QA execution",
};

export const QA_STATUS_LABELS: Record<DeliveryQAStatus["status"], string> = {
  accepted: "Accepted",
  needs_fixes: "Needs fixes",
  outdated: "Outdated",
  queued: "Queued",
  running: "Running",
  error: "QA error",
  never: "Never run",
};

export function deliveryQAStatus(
  row: DeliveryTaskBoardRow,
  cutoff: number
): DeliveryQAStatus {
  const qa = row.qa;
  if (
    (qa.status === "accepted" || qa.status === "needs_fixes") &&
    (!qa.finished_at || new Date(qa.finished_at).getTime() < cutoff)
  ) {
    return {
      ...qa,
      status: "outdated",
      detail: "Last completed QA is outside the selected time window",
    };
  }
  return qa;
}

export function deliveryNextAction(
  row: DeliveryTaskBoardRow,
  status: DeliveryQAStatus["status"]
): string {
  switch (status) {
    case "never":
      return "Run QA";
    case "outdated":
      return "Rerun QA";
    case "queued":
    case "running":
      return "Wait for QA";
    case "error":
      return "Retry QA job";
    case "needs_fixes":
      return "Review and fix";
    case "accepted":
      return row.ready ? "Ready to deliver" : "Review checks / sign off";
  }
}

/** Tailwind classes for one check-status dot/chip. */
export function checkTone(status: DeliveryCheckStatus): string {
  switch (status) {
    case "pass":
      return "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400";
    case "fail":
      return "bg-red-500/15 text-red-700 dark:text-red-400";
    case "waived":
      return "bg-amber-500/15 text-amber-700 dark:text-amber-400";
    default:
      return "bg-muted text-muted-foreground";
  }
}

/** One-line readiness summary for a board header. */
export function readySummary(board: DeliveryBoardResponse): string {
  const base = `${board.ready_task_count}/${board.task_count} tasks ready`;
  const failingDeliveryChecks = board.delivery_checks.filter(
    (check) => check.status === "fail"
  ).length;
  if (failingDeliveryChecks > 0) {
    return `${base} · ${failingDeliveryChecks} delivery check${
      failingDeliveryChecks === 1 ? "" : "s"
    } open`;
  }
  return base;
}

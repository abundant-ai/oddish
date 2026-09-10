import type {
  DeliveryBoardResponse,
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
  accepted: "No blocking defects found",
  needs_fixes: "Blocking defects found",
  outdated: "Review needs refresh",
  queued: "Review queued",
  running: "Review running",
  error: "Review could not complete",
  never: "Not reviewed",
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

/** Delivery blockers remain separate from review completion and recorded sign-off. */
export function isDeliveryBlocked(row: DeliveryTaskBoardRow): boolean {
  return (
    row.checks.some(
      (check) => check.kind === "automated" && check.status === "fail"
    ) || row.defects.some((defect) => !defect.acknowledged)
  );
}

/** Disjoint owner-chart outcomes; these do not determine delivery readiness. */
export function deliveryOwnerOutcome(row: DeliveryTaskBoardRow): {
  status:
    | "needs_work"
    | "qa_incomplete"
    | "qa_accepted"
    | "accepted_exceptions";
  signedOff: boolean;
} {
  // The backend only passes this check for the displayed version's sign-off.
  const signedOff = row.checks.some(
    (check) =>
      check.key === "signoff" &&
      check.kind === "manual" &&
      check.status === "pass"
  );
  const openFindings = row.defects.some((finding) => !finding.acknowledged);
  if (
    row.qa.status === "needs_fixes" &&
    signedOff &&
    row.defects.length > 0 &&
    !openFindings
  ) {
    return { status: "accepted_exceptions", signedOff };
  }
  if (openFindings || row.qa.status === "needs_fixes") {
    return { status: "needs_work", signedOff };
  }
  return {
    status: row.qa.status === "accepted" ? "qa_accepted" : "qa_incomplete",
    signedOff,
  };
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

/** Shareable delivery view. Page numbers in URLs are one-based. */
export type DeliveryTaskFilter =
  | "all"
  | "outstanding"
  | "blocked"
  | "awaiting_signoff"
  | "ready";

export const DELIVERY_PAGE_SIZES = [10, 25, 50, 100];

export function parseDeliveryView(params: Pick<URLSearchParams, "get">) {
  const filter = params.get("filter");
  const qa = params.get("qa");
  const issue = params.get("issue");
  const owner = params.get("owner");
  const group = params.get("group");
  const rawPage = params.get("page") ?? "1";
  const page = Number(rawPage);
  const pageSize = Number(params.get("per_page"));
  return {
    pageSize: DELIVERY_PAGE_SIZES.includes(pageSize) ? pageSize : 25,
    page:
      /^\d+$/.test(rawPage) && Number.isSafeInteger(page) && page > 0
        ? page - 1
        : 0,
    filter: ([
      "all",
      "outstanding",
      "blocked",
      "awaiting_signoff",
      "ready",
    ].includes(filter ?? "")
      ? filter
      : "outstanding") as DeliveryTaskFilter,
    qaDays: params.get("days") === "1" ? "1" : "7",
    qaFilter:
      qa &&
      (Object.hasOwn(QA_STATUS_LABELS, qa) ||
        qa === "checked" ||
        qa === "needs_qa")
        ? qa
        : "all",
    issueFilter: issue && Object.hasOwn(QA_ISSUE_LABELS, issue) ? issue : "all",
    ownerFilter: owner && owner !== "all" ? owner : "all",
    groupBy: group === "owner" || group === "issue" ? group : "none",
    focusTask: params.get("task") || null,
  };
}

/** Preserve unrelated URL parameters, omitting default view values. */
export function deliveryViewQuery(
  current: string,
  patch: Partial<
    Record<
      | "page"
      | "per_page"
      | "filter"
      | "days"
      | "qa"
      | "issue"
      | "owner"
      | "group"
      | "task"
      | "panels",
      string | null
    >
  >
) {
  const params = new URLSearchParams(current);
  const defaults: Record<string, string> = {
    page: "1",
    per_page: "25",
    filter: "outstanding",
    days: "7",
    qa: "all",
    issue: "all",
    owner: "all",
    group: "none",
  };
  for (const [key, value] of Object.entries(patch)) {
    if (!value || value === defaults[key]) params.delete(key);
    else params.set(key, value);
  }
  const query = params.toString();
  return query ? `?${query}` : "";
}

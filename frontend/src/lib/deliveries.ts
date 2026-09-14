import type {
  DeliveryBoardResponse,
  DeliveryPageRow,
  DeliveryPageResponse,
  DeliveryQAStatus,
  DeliveryTaskBoardRow,
  QAIssueCategory,
} from "@/lib/types";

export const QA_ISSUE_LABELS: Record<QAIssueCategory, string> = {
  instructions: "Instructions",
  verifier: "Verifier / grading",
  environment: "Environment / runtime",
  evidence: "Missing evidence",
  qa_execution: "Verdict generation",
};

export const DELIVERY_CHECK_STATUS_LABELS: Record<
  DeliveryQAStatus["status"],
  string
> = {
  accepted: "Delivery checks passed",
  needs_fixes: "Delivery checks blocked",
  outdated: "Delivery checks need refresh",
  queued: "Delivery checks queued",
  running: "Delivery checks running",
  error: "Delivery checks incomplete",
  never: "Delivery checks not run",
};

export const DELIVERY_STATES = {
  needs_work: {
    label: "Needs work",
    tone: "text-red-700 dark:text-red-400",
    background: "bg-red-500/10",
  },
  qa_incomplete: {
    label: "Checks needed",
    tone: "text-amber-700 dark:text-amber-400",
    background: "bg-amber-500/10",
  },
  awaiting_signoff: {
    label: "Needs sign-off",
    tone: "text-blue-700 dark:text-blue-400",
    background: "bg-blue-500/10",
  },
  ready: {
    label: "Ready",
    tone: "text-emerald-700 dark:text-emerald-400",
    background: "bg-emerald-500/10",
  },
} as const;

export type DeliveryTaskState = keyof typeof DELIVERY_STATES;

/** One state per task, based on delivery requirements rather than review age.
 * Waived checks and acknowledged findings still permit readiness. */
export function deliveryTaskState(
  row: DeliveryTaskBoardRow | DeliveryPageRow
): DeliveryTaskState {
  if ("state" in row) return row.state;
  if (row.defects.some((finding) => !finding.acknowledged)) return "needs_work";
  const failedChecks = row.checks.filter(
    (check) => check.kind === "automated" && check.status === "fail"
  );
  if (
    failedChecks.some(
      (check) =>
        check.key === "task_exists" ||
        (check.key === "verdict_ok" && row.qa.status === "needs_fixes")
    )
  ) {
    return "needs_work";
  }
  if (failedChecks.length) return "qa_incomplete";
  return row.ready ? "ready" : "awaiting_signoff";
}

/** Keep missing delivery requirements visible without parsing check prose. */
export function deliveryTaskLabels(row: DeliveryTaskBoardRow): string[] {
  const defects = row.defects.filter((finding) => !finding.acknowledged).length;
  if (defects > 0) return [`Rejected: ${defects} Must Fix`];
  const labels = row.checks
    .filter((check) => check.kind === "automated" && check.status === "fail")
    .flatMap((check) =>
      check.failure_labels?.length
        ? check.failure_labels
        : [
            {
              pre_trial_passed: "Pre-trial audit needed",
              min_rollouts: "Run requirements unmet",
              verdict_ok:
                row.qa.status === "needs_fixes" ? "Rejected" : "No verdict",
              task_exists: "Task missing",
              no_must_fix: "Finding decisions needed",
            }[check.key] ?? check.label,
          ]
    );
  return labels.length
    ? [...new Set(labels)]
    : [DELIVERY_STATES[deliveryTaskState(row)].label];
}

/** Ownership scopes current counts and rows, including completed tasks. */
export function deliveryOwnerTasks(
  board: DeliveryBoardResponse,
  owner: string
) {
  if (owner === "all") return board.tasks;
  const id = owner === "mine" ? board.qa_viewer_user_id : owner;
  return board.tasks.filter((row) =>
    owner === "unassigned"
      ? !row.qa_work.owner_user_id
      : !!id && row.qa_work.owner_user_id === id
  );
}

/** Missing owner snapshots and missing dates remain gaps, never inferred zeros. */
export function deliveryProgressHistory(
  board: DeliveryBoardResponse,
  owner: string
) {
  const id = owner === "mine" ? board.qa_viewer_user_id : owner;
  const observations = (board.progress_history ?? []).flatMap((point) => {
    if (owner === "all")
      return [
        {
          date: point.recorded_at.slice(0, 10),
          task_count: point.task_count,
          ready: point.ready,
        },
      ];
    if (point.owners == null || !id) return [];
    const counts = point.owners[id] ?? { task_count: 0, ready: 0 };
    return [{ date: point.recorded_at.slice(0, 10), ...counts }];
  });
  if (!observations.length) return [];
  const byDay = new Map(observations.map((point) => [point.date, point]));
  const start = Date.parse(observations[0].date + "T00:00:00Z");
  const end = Date.parse(
    (board.finalized_at ?? board.qa_as_of ?? observations.at(-1)!.date).slice(
      0,
      10
    ) + "T00:00:00Z"
  );
  const days: {
    date: string;
    task_count: number | null;
    ready: number | null;
  }[] = [];
  for (let time = start; time <= end; time += 86400000) {
    const date = new Date(time).toISOString().slice(0, 10);
    days.push(byDay.get(date) ?? { date, task_count: null, ready: null });
  }
  return days;
}

/** Shareable delivery view. Page numbers in URLs are one-based. */
export type DeliveryTaskFilter =
  | DeliveryTaskState
  | "all"
  | "outstanding"
  | "blocked";

export const DELIVERY_PAGE_SIZES = [10, 25, 50, 100];

export function parseDeliveryView(params: Pick<URLSearchParams, "get">) {
  const filter = params.get("filter");
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
    filter: (filter &&
    (Object.hasOwn(DELIVERY_STATES, filter) ||
      filter === "outstanding" ||
      filter === "blocked")
      ? filter
      : "all") as DeliveryTaskFilter,
    issueFilter: issue && Object.hasOwn(QA_ISSUE_LABELS, issue) ? issue : "all",
    ownerFilter: owner && owner !== "all" ? owner : "all",
    groupBy:
      group === "owner" || group === "issue" || group === "state"
        ? group
        : "none",
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
    filter: "all",
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

/** Only parameters affecting returned rows identify a page request/cache entry.
 * Disclosure state and unrelated link parameters remain in browser history. */
export function deliveryPageQuery(params: Pick<URLSearchParams, "get">) {
  const view = parseDeliveryView(params);
  return deliveryViewQuery("", {
    page: String(view.page + 1),
    per_page: String(view.pageSize),
    filter: view.filter,
    issue: view.issueFilter,
    owner: view.ownerFilter,
    group: view.groupBy,
    task: view.focusTask,
  });
}

/** IDs across the full delivery take precedence over legacy task names,
 * including when the matching member is not on the loaded page. */
export function focusedDeliveryTask(
  page: DeliveryPageResponse,
  focusTask: string | null
) {
  return (
    page.tasks.find((row) => row.task_id === focusTask) ??
    (!page.member_task_ids.includes(focusTask ?? "")
      ? page.tasks.find((row) => row.task_name === focusTask)
      : undefined)
  );
}

/** Reuse a loaded table when only its expanded row changes. Off-page links and
 * filter exceptions still use the server to locate the correct page. */
export function deliveryPageContainsView(
  page: DeliveryPageResponse,
  loadedQuery: string,
  requestedQuery: string
): boolean {
  const loaded = parseDeliveryView(new URLSearchParams(loadedQuery));
  const requested = parseDeliveryView(new URLSearchParams(requestedQuery));
  if (
    loaded.pageSize !== requested.pageSize ||
    loaded.filter !== requested.filter ||
    loaded.issueFilter !== requested.issueFilter ||
    loaded.ownerFilter !== requested.ownerFilter ||
    loaded.groupBy !== requested.groupBy
  )
    return false;
  const focus = focusedDeliveryTask(page, requested.focusTask);
  if (page.focus_outside_filters && focus?.task_id !== page.focus_task_id)
    return false;
  if (requested.focusTask) return Boolean(focus);
  return (
    page.page ===
    Math.min(
      requested.page + 1,
      Math.max(1, Math.ceil(page.total / requested.pageSize))
    )
  );
}

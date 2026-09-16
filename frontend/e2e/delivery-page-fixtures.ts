import type {
  DeliveryBoardResponse,
  DeliveryPageResponse,
  DeliverySelectionItem,
  DeliveryTaskBoardRow,
  QAIssueCategory,
} from "../src/lib/types";
import {
  parseDeliveryView,
  deliveryOwnerTasks,
  deliveryTaskState,
  DELIVERY_STATES,
  QA_ISSUE_LABELS,
} from "../src/lib/deliveries";

/** Emulate the paginated HTTP contract; backend parity is tested in PostgreSQL. */
export function pageFixture(
  board: DeliveryBoardResponse,
  params: URLSearchParams
): DeliveryPageResponse {
  const view = parseDeliveryView(params);
  const owned = deliveryOwnerTasks(board, view.ownerFilter);
  const counts = {
    needs_work: 0,
    qa_incomplete: 0,
    awaiting_signoff: 0,
    ready: 0,
  };
  for (const row of owned) counts[deliveryTaskState(row)]++;
  const rows = owned.filter((row) => {
    const state = deliveryTaskState(row);
    return (
      (view.filter === "all" ||
        view.filter === state ||
        (view.filter === "outstanding" && !row.ready) ||
        (view.filter === "blocked" &&
          ["needs_work", "qa_incomplete"].includes(state))) &&
      (view.issueFilter === "all" ||
        row.qa_work.issue_categories.includes(
          view.issueFilter as QAIssueCategory
        ))
    );
  });
  const focus =
    board.tasks.find((r) => r.task_id === view.focusTask) ??
    board.tasks.find((r) => r.task_name === view.focusTask);
  const outside = Boolean(focus && !rows.includes(focus));
  if (focus && outside) rows.push(focus);
  if (view.groupBy !== "none") {
    const groupLabel = (row: DeliveryTaskBoardRow) =>
      view.groupBy === "owner"
        ? (row.qa_owner_name ?? row.qa_work.owner_user_id ?? "Unassigned")
        : view.groupBy === "state"
          ? DELIVERY_STATES[deliveryTaskState(row)].label
          : row.qa_work.issue_categories[0]
            ? QA_ISSUE_LABELS[row.qa_work.issue_categories[0]]
            : "Uncategorized";
    rows.sort((a, b) => groupLabel(a).localeCompare(groupLabel(b)));
  }
  const page = focus
    ? Math.floor(rows.indexOf(focus) / view.pageSize)
    : Math.min(
        view.page,
        Math.max(0, Math.ceil(rows.length / view.pageSize) - 1)
      );
  return {
    ...board,
    task_count: board.tasks.length,
    tasks: rows
      .slice(page * view.pageSize, (page + 1) * view.pageSize)
      .map((row) => ({ ...row, state: deliveryTaskState(row) })),
    page: page + 1,
    per_page: view.pageSize,
    total: rows.length,
    focus_task_id: focus?.task_id ?? null,
    focus_outside_filters: outside,
    owner_counts: counts,
    owners: Object.fromEntries(
      board.tasks
        .filter((r) => r.qa_work.owner_user_id)
        .map((r) => [
          r.qa_work.owner_user_id!,
          r.qa_owner_name ?? r.qa_work.owner_user_id!,
        ])
    ),
    member_task_ids: board.tasks.map((r) => r.task_id),
    matching_task_ids: rows.map((r) => r.delivery_task_id),
  };
}

export function selectionFixture(
  board: DeliveryBoardResponse,
  params: URLSearchParams
): DeliverySelectionItem[] {
  const ids = pageFixture(board, params).matching_task_ids;
  return ids.map((id) => {
    const row = board.tasks.find((r) => r.delivery_task_id === id)!;
    return {
      delivery_task_id: row.delivery_task_id,
      task_id: row.task_id,
      task_name: row.task_name,
      version_id: row.version_id ?? null,
      version: row.version ?? null,
      state: deliveryTaskState(row),
      qa_status: row.qa.status,
      can_sign_off:
        deliveryTaskState(row) === "awaiting_signoff" &&
        row.checks.some((c) => c.key === "signoff" && c.status === "fail"),
    };
  });
}

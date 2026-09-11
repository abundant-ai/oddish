"""Page delivery evidence without changing complete-board or approval contracts."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from oddish.core.task_findings import task_defect_items
from oddish.db import TaskVersionModel
from oddish.schemas import (
    DeliveryBoardResponse,
    DeliveryPageResponse,
    DeliveryPageRow,
    DeliverySelectionItem,
    DeliveryTaskBoardRow,
    DeliveryTaskState,
    DeliveryViewQuery,
)

STATE_LABELS = {
    "needs_work": "Needs work",
    "qa_incomplete": "QA incomplete",
    "awaiting_signoff": "Needs sign-off",
    "ready": "Ready",
}
ISSUE_LABELS = {
    "instructions": "Instructions",
    "verifier": "Verifier / grading",
    "environment": "Environment / runtime",
    "evidence": "Missing evidence",
    "qa_execution": "QA execution",
}


def delivery_task_state(row: DeliveryTaskBoardRow) -> DeliveryTaskState:
    if any(not d.acknowledged for d in row.defects):
        return "needs_work"
    failed = [c for c in row.checks if c.kind == "automated" and c.status == "fail"]
    if any(
        c.key == "task_exists"
        or (c.key == "verdict_ok" and row.qa.status == "needs_fixes")
        for c in failed
    ):
        return "needs_work"
    if failed:
        return "qa_incomplete"
    return "ready" if row.ready else "awaiting_signoff"


def delivery_view_rows(board: DeliveryBoardResponse, view: DeliveryViewQuery):
    owner = board.qa_viewer_user_id if view.owner == "mine" else view.owner
    owned = [
        r
        for r in board.tasks
        if view.owner == "all"
        or (
            not r.qa_work.owner_user_id
            if owner == "unassigned"
            else bool(owner) and r.qa_work.owner_user_id == owner
        )
    ]
    counts = {state: 0 for state in STATE_LABELS}
    rows = []
    for row in owned:
        state = delivery_task_state(row)
        counts[state] += 1
        if (
            view.filter == "all"
            or view.filter == state
            or (view.filter == "outstanding" and not row.ready)
            or (view.filter == "blocked" and state in ("needs_work", "qa_incomplete"))
        ) and (view.issue == "all" or view.issue in row.qa_work.issue_categories):
            rows.append(row)
    # IDs take precedence over old task-name links. A focused task stays in
    # the view even after a save moves it outside the selected filters.
    focus = next((r for r in board.tasks if r.task_id == view.task), None)
    if focus is None:
        focus = next((r for r in board.tasks if r.task_name == view.task), None)
    outside = focus is not None and focus not in rows
    if outside:
        rows.append(focus)
    if view.group != "none":

        def group_label(row):
            if view.group == "owner":
                return row.qa_owner_name or row.qa_work.owner_user_id or "Unassigned"
            if view.group == "state":
                return STATE_LABELS[delivery_task_state(row)]
            return (
                ISSUE_LABELS[row.qa_work.issue_categories[0]]
                if row.qa_work.issue_categories
                else "Uncategorized"
            )

        rows.sort(key=lambda row: group_label(row).casefold())
    return rows, counts, focus, outside


def delivery_selection(
    board: DeliveryBoardResponse, view: DeliveryViewQuery
) -> list[DeliverySelectionItem]:
    rows, _, _, _ = delivery_view_rows(board, view)
    return [
        DeliverySelectionItem(
            delivery_task_id=r.delivery_task_id,
            task_id=r.task_id,
            task_name=r.task_name,
            version_id=r.version_id,
            version=r.version,
            state=delivery_task_state(r),
            can_sign_off=delivery_task_state(r) == "awaiting_signoff"
            and any(c.key == "signoff" and c.status == "fail" for c in r.checks),
            qa_status=r.qa.status,
        )
        for r in rows
    ]


async def delivery_page(
    session: AsyncSession, board: DeliveryBoardResponse, view: DeliveryViewQuery
) -> DeliveryPageResponse:
    rows, counts, focus, outside = delivery_view_rows(board, view)
    page = min(view.page, max(1, (len(rows) + view.per_page - 1) // view.per_page))
    if focus is not None:
        page = rows.index(focus) // view.per_page + 1
    visible = rows[(page - 1) * view.per_page : page * view.per_page]
    if not board.frozen:
        # Only the expanded version needs full evidence. The compact pass and this
        # hydration use the same collector, defect IDs and precedence rules.
        version_ids = (
            {focus.version_id}
            if focus and focus.version_id and focus.defects
            else set()
        )
        if version_ids:
            versions = (
                await session.scalars(
                    select(TaskVersionModel)
                    .where(TaskVersionModel.id.in_(version_ids))
                    .options(
                        load_only(
                            TaskVersionModel.id,
                            TaskVersionModel.pre_trial,
                            TaskVersionModel.reported_findings,
                        )
                    )
                    .execution_options(include_deleted=True)
                )
            ).all()
            findings = await task_defect_items(session, {v.id: v for v in versions})
            for row in visible:
                bodies = {
                    d["id"]: d["finding"] for d in findings.get(row.version_id, [])
                }
                row.defects = [
                    d.model_copy(update={"finding": bodies.get(d.id)})
                    for d in row.defects
                ]
    return DeliveryPageResponse(
        **board.model_dump(exclude={"tasks"}),
        tasks=[
            DeliveryPageRow(**r.model_dump(), state=delivery_task_state(r))
            for r in visible
        ],
        page=page,
        per_page=view.per_page,
        total=len(rows),
        focus_task_id=focus.task_id if focus else None,
        focus_outside_filters=outside,
        owner_counts=counts,
        owners={
            r.qa_work.owner_user_id: r.qa_owner_name or r.qa_work.owner_user_id
            for r in board.tasks
            if r.qa_work.owner_user_id
        },
        member_task_ids=[r.task_id for r in board.tasks],
        matching_task_ids=[r.delivery_task_id for r in rows],
    )

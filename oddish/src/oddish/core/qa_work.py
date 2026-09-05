"""Assign QA review work to current task versions, independently of deliveries."""

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db import TaskModel, TaskVersionModel, utcnow
from oddish.schemas import QAWorkAssignResponse, QAWorkMetadata


async def assign_task_qa_work_core(
    session: AsyncSession,
    *,
    org_id: str | None,
    task_ids: list[str],
    owner_user_id: str,
    replace: bool = False,
) -> QAWorkAssignResponse:
    # Lock tasks before versions, as version changes do. Stable lock ordering
    # serializes overlapping batches without retargeting a task during assignment.
    tasks = {
        task.id: task
        for task in await session.scalars(
            select(TaskModel)
            .where(TaskModel.org_id == org_id, TaskModel.id.in_(task_ids))
            .order_by(TaskModel.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    task_ids = list(dict.fromkeys(task_ids))
    missing = [task_id for task_id in task_ids if task_id not in tasks]
    if missing:
        raise HTTPException(
            status_code=404, detail=f"Tasks not found: {', '.join(missing)}"
        )
    versions = {
        version.id: version
        for version in await session.scalars(
            select(TaskVersionModel)
            .where(
                TaskVersionModel.id.in_(
                    [task.current_version_id for task in tasks.values()]
                )
            )
            .order_by(TaskVersionModel.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    without_version = [
        task_id
        for task_id in task_ids
        if tasks[task_id].current_version_id not in versions
    ]
    if without_version:
        raise HTTPException(
            status_code=409,
            detail=f"Tasks have no current version: {', '.join(without_version)}",
        )

    result = QAWorkAssignResponse(owner_user_id=owner_user_id)
    now = utcnow()
    for task_id in task_ids:
        version = versions[tasks[task_id].current_version_id]
        work = QAWorkMetadata.model_validate(version.qa_work or {})
        if work.owner_user_id == owner_user_id:
            result.unchanged_task_ids.append(task_id)
        elif work.owner_user_id and not replace:
            result.skipped_task_ids.append(task_id)
        else:
            work.owner_user_id, work.claimed_at = owner_user_id, now
            version.qa_work = work.model_dump(mode="json")
            result.assigned_task_ids.append(task_id)
    await session.flush()
    return result

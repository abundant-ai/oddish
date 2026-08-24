from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db import TaskModel, TaskVersionModel


async def resolve_task_file_source(
    session: AsyncSession,
    *,
    task_id: str,
    version: int | None,
    org_id: str | None = None,
) -> tuple[int | None, str | None]:
    """Authorize a task and select the exact version source used for file reads."""
    version_join = (
        TaskVersionModel.id == TaskModel.current_version_id
        if version is None
        else and_(
            TaskVersionModel.task_id == TaskModel.id,
            TaskVersionModel.version == version,
        )
    )
    query = select(
        TaskVersionModel.version,
        TaskVersionModel.task_s3_key.label("version_s3_key"),
        TaskModel.task_s3_key.label("legacy_task_s3_key"),
    ).select_from(TaskModel)
    query = (
        query.outerjoin(TaskVersionModel, version_join)
        if version is None
        else query.join(TaskVersionModel, version_join)
    )
    query = query.where(TaskModel.id == task_id)
    if org_id is not None:
        query = query.where(TaskModel.org_id == org_id)

    row = (await session.execute(query)).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    prefix = row.version_s3_key if row.version is not None else row.legacy_task_s3_key
    return row.version, str(prefix) if prefix else None

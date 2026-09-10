from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db import TaskModel, TaskVersionModel


@dataclass(frozen=True)
class TaskFileSource:
    """One authorized database snapshot for a file read, including its revision."""

    version: int | None
    task_s3_prefix: str | None
    expanded_manifest_key: str | None
    content_hash: str | None

    @property
    def expanded(self) -> bool | None:
        return bool(self.expanded_manifest_key) if self.version is not None else None


async def resolve_task_file_source(
    session: AsyncSession,
    *,
    task_id: str,
    version: int | None,
    org_id: str | None = None,
) -> TaskFileSource:
    """Authorize a task and select the exact version source used for file reads.

    Archive, manifest, and content fingerprint come from the same version row.
    Storage still validates legacy manifests against the selected archive.
    """

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
        TaskVersionModel.expanded_manifest_key,
        TaskVersionModel.content_hash,
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
    # A version without a stored archive pointer may use its canonical version
    # directory. Never let storage's legacy fallback substitute the task-wide
    # archive: it does not prove which version those bytes belong to.
    prefix = (
        row.version_s3_key or f"tasks/{task_id}/v{row.version}/"
        if row.version is not None
        else row.legacy_task_s3_key
    )
    return TaskFileSource(
        row.version,
        str(prefix) if prefix else None,
        row.expanded_manifest_key,
        row.content_hash,
    )

"""Resolve a task's source location for the pre-trial audit.

Both pre-trial execution paths -- the built-in synth (backend) and the
registry-assignment runner (oddish worker) -- need the ``(task_s3_key,
task_path)`` pair to feed ``resolve_task_directory``. Sharing one resolver keeps
their version-pinning and error behavior from drifting.
"""

from __future__ import annotations

from sqlalchemy import select

from oddish.db import get_session
from oddish.db.models import TaskModel, TaskVersionModel


async def resolve_task_source_location(
    task_id: str, task_version_id: str | None = None
) -> tuple[str | None, str | None]:
    """The ``(task_s3_key, task_path)`` of a task's source.

    Pins to a specific (immutable) version when ``task_version_id`` is given, so
    a re-upload can't swap the source under a version-scoped audit; otherwise
    falls back to the task's latest-version mirror. Raises if the row is gone.
    """
    async with get_session() as session:
        if task_version_id:
            row = (
                await session.execute(
                    select(
                        TaskVersionModel.task_s3_key, TaskVersionModel.task_path
                    ).where(TaskVersionModel.id == task_version_id)
                )
            ).first()
        else:
            row = (
                await session.execute(
                    select(TaskModel.task_s3_key, TaskModel.task_path).where(
                        TaskModel.id == task_id
                    )
                )
            ).first()
    if row is None:
        raise RuntimeError(f"task source not found for pre-trial QA (task {task_id})")
    return row.task_s3_key, row.task_path

"""Resolve a task's database-selected source location.

Both pre-trial execution paths -- the built-in synth (backend) and the
registry-assignment runner (oddish worker) -- need the ``(task_s3_key,
task_path)`` pair to feed ``resolve_task_directory``. Sharing one resolver keeps
their version-pinning and error behavior from drifting. Other consumers that
must honor an in-place overwrite, such as GKE image builds, use it as well.
"""

from __future__ import annotations

from sqlalchemy import select

from oddish.db import get_session
from oddish.db.models import TaskModel, TaskVersionModel


async def resolve_task_source_location(
    task_id: str, task_version_id: str | None = None
) -> tuple[str | None, str | None]:
    """The ``(task_s3_key, task_path)`` of a task's source.

    Pins to a specific selected version when ``task_version_id`` is given;
    in-place overwrites atomically switch that version row to its replacement
    archive. Otherwise falls back to the task's current-version mirror. Raises
    if the row is gone.
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

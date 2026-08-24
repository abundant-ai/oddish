from __future__ import annotations

import uuid

from fastapi import HTTPException
import pytest

from oddish.core.task_files import resolve_task_file_source
from oddish.db import TaskModel, TaskVersionModel


@pytest.mark.asyncio
async def test_task_file_source_selects_exact_authorized_version(session) -> None:
    suffix = uuid.uuid4().hex[:8]
    task = TaskModel(
        id=f"task-{suffix}",
        name=f"task-{suffix}",
        org_id="org-1",
        user="tester",
        task_path="source",
    )
    session.add(task)
    await session.flush()

    current = TaskVersionModel(
        id=f"{task.id}-v1",
        task_id=task.id,
        version=1,
        task_path="source-v1",
        task_s3_key=f"tasks/{task.id}/v1-revisions/current/",
    )
    historical = TaskVersionModel(
        id=f"{task.id}-v2",
        task_id=task.id,
        version=2,
        task_path="source-v2",
        task_s3_key=f"tasks/{task.id}/v2-revisions/historical/",
    )
    session.add_all([current, historical])
    await session.flush()
    task.current_version_id = current.id
    await session.flush()

    assert await resolve_task_file_source(
        session, task_id=task.id, org_id="org-1", version=None
    ) == (1, current.task_s3_key)
    assert await resolve_task_file_source(
        session, task_id=task.id, org_id="org-1", version=2
    ) == (2, historical.task_s3_key)

    for org_id, version in [("org-2", None), ("org-1", 3)]:
        with pytest.raises(HTTPException) as exc:
            await resolve_task_file_source(
                session, task_id=task.id, org_id=org_id, version=version
            )
        assert exc.value.status_code == 404

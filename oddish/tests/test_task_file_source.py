from __future__ import annotations

import uuid

from fastapi import HTTPException
import pytest

from oddish.core.task_files import TaskFileSource, resolve_task_file_source
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
    ) == TaskFileSource(1, current.task_s3_key, None, None)
    assert await resolve_task_file_source(
        session, task_id=task.id, org_id="org-1", version=2
    ) == TaskFileSource(2, historical.task_s3_key, None, None)

    # The expand worker's stamp is the reader's answer to "is the per-file
    # tree in sync with this archive?"; an overwrite clears it again.
    historical.expanded_manifest_key = f"tasks/{task.id}/v2-files/.oddish-manifest.json"
    await session.flush()
    assert await resolve_task_file_source(
        session, task_id=task.id, org_id="org-1", version=2
    ) == TaskFileSource(
        2, historical.task_s3_key, historical.expanded_manifest_key, None
    )

    for org_id, version in [("org-2", None), ("org-1", 3)]:
        with pytest.raises(HTTPException) as exc:
            await resolve_task_file_source(
                session, task_id=task.id, org_id=org_id, version=version
            )
        assert exc.value.status_code == 404

    # Missing historical source metadata must not read today's task archive.
    task.task_s3_key = f"tasks/{task.id}/current/"
    historical.task_s3_key = None
    historical.expanded_manifest_key = None
    await session.flush()
    source = await resolve_task_file_source(
        session, task_id=task.id, org_id="org-1", version=2
    )
    assert source.task_s3_prefix == f"tasks/{task.id}/v2/"

    from unittest.mock import AsyncMock
    from oddish.db.storage import StorageClient

    storage = object.__new__(StorageClient)
    storage.object_exists = AsyncMock(return_value=False)
    root, archive = await storage._resolve_task_prefix(
        task.id, source.version, source.task_s3_prefix
    )
    assert root == f"tasks/{task.id}/v2/"
    assert archive.startswith(root)
    storage.object_exists.assert_not_awaited()

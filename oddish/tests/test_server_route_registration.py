"""Standalone server mounts the same contracts as the hosted backend.

Guards the shared-core contract for the experiment-options route: both servers
must expose it (backend/api/routers/tasks.py has the hosted twin).
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from oddish.server import api, list_task_files


def test_experiment_options_route_mounted() -> None:
    paths = [getattr(route, "path", None) for route in api.routes]

    assert "/tasks/browse/experiment-options" in paths


@pytest.mark.asyncio
async def test_task_tree_forwards_inline_flag() -> None:
    session = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                scalar_one_or_none=lambda: SimpleNamespace(current_version=None)
            )
        )
    )

    @asynccontextmanager
    async def fake_get_session():
        yield session

    list_files = AsyncMock(return_value={"files": []})
    with (
        patch("oddish.server.get_session", new=fake_get_session),
        patch("oddish.server.list_task_files_s3", new=list_files),
    ):
        await list_task_files(
            "task-1",
            prefix=None,
            recursive=True,
            limit=1000,
            cursor=None,
            presign=False,
            inline=False,
            version=3,
            stream=False,
        )

    list_files.assert_awaited_once_with(
        task_id="task-1",
        prefix=None,
        recursive=True,
        limit=1000,
        cursor=None,
        presign=False,
        version=3,
        inline=False,
    )

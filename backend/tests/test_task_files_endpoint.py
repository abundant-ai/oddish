"""GET /tasks/{id}/files request-shape contract."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from api.app import create_app


@pytest.fixture
def client():
    from auth import APIKeyScope, AuthContext, AuthMethod, require_auth

    fake_auth = AuthContext(
        method=AuthMethod.API_KEY,
        org_id="org-1",
        user_id="u-1",
        scope=APIKeyScope.READ,
    )

    async def fake_require_auth():
        return fake_auth

    app = create_app()
    app.dependency_overrides[require_auth] = fake_require_auth
    return TestClient(app)


def test_tree_only_listing_forwards_inline_and_presign_flags(client):
    @asynccontextmanager
    async def fake_get_session():
        yield object()

    get_task = AsyncMock(return_value=SimpleNamespace(current_version=None))
    list_files = AsyncMock(
        return_value={
            "task_id": "task-1",
            "files": [{"path": "instruction.md", "size": 12}],
            "dirs": [],
            "recursive": True,
            "presigned": False,
        }
    )

    with (
        patch("api.routers.tasks.get_session", new=fake_get_session),
        patch("api.routers.tasks.get_task_for_org_core", new=get_task),
        patch("api.routers.tasks.list_task_files_s3", new=list_files),
    ):
        response = client.get(
            "/tasks/task-1/files?recursive=1&version=3&inline=false&presign=false"
        )

    assert response.status_code == 200
    assert response.json()["files"] == [{"path": "instruction.md", "size": 12}]
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

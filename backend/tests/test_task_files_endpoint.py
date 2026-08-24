"""GET /tasks/{id}/files request-shape contract."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
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
    async def fake_get_read_session():
        yield object()

    resolve_source = AsyncMock(return_value=(3, "tasks/task-1/v3/"))
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
        patch("api.routers.tasks.get_read_session", new=fake_get_read_session),
        patch("api.routers.tasks.resolve_task_file_source", new=resolve_source),
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
        task_s3_prefix="tasks/task-1/v3/",
    )


def test_directory_page_forwards_prefix_limit_and_cursor(client):
    @asynccontextmanager
    async def fake_get_read_session():
        yield object()

    resolve_source = AsyncMock(return_value=(3, "tasks/task-1/v3/"))
    list_files = AsyncMock(
        return_value={
            "task_id": "task-1",
            "files": [{"path": "environment/Dockerfile", "size": 12}],
            "dirs": [{"path": "environment/repo"}],
            "recursive": False,
            "cursor": "next-page",
            "truncated": True,
            "presigned": False,
        }
    )

    with (
        patch("api.routers.tasks.get_read_session", new=fake_get_read_session),
        patch("api.routers.tasks.resolve_task_file_source", new=resolve_source),
        patch("api.routers.tasks.list_task_files_s3", new=list_files),
    ):
        response = client.get(
            "/tasks/task-1/files?recursive=0&prefix=environment&limit=100"
            "&cursor=page-1&version=3&inline=false&presign=false"
        )

    assert response.status_code == 200
    assert response.json()["cursor"] == "next-page"
    list_files.assert_awaited_once_with(
        task_id="task-1",
        prefix="environment",
        recursive=False,
        limit=100,
        cursor="page-1",
        presign=False,
        version=3,
        inline=False,
        task_s3_prefix="tasks/task-1/v3/",
    )


def test_selected_file_forwards_preview_limit(client):
    @asynccontextmanager
    async def fake_get_read_session():
        yield object()

    resolve_source = AsyncMock(return_value=(3, "tasks/task-1/v3/"))
    get_file = AsyncMock(
        return_value={
            "path": "large.txt",
            "content": "preview",
            "size": 2_000_000,
            "is_truncated": True,
        }
    )

    with (
        patch("api.routers.tasks.get_read_session", new=fake_get_read_session),
        patch("api.routers.tasks.resolve_task_file_source", new=resolve_source),
        patch("api.routers.tasks.get_task_file_content_s3", new=get_file),
    ):
        response = client.get(
            "/tasks/task-1/files/large.txt?version=3&max_bytes=102400"
        )

    assert response.status_code == 200
    assert response.json()["is_truncated"] is True
    get_file.assert_awaited_once_with(
        task_id="task-1",
        file_path="large.txt",
        presign=False,
        version=3,
        max_bytes=102400,
        task_s3_prefix="tasks/task-1/v3/",
    )


@pytest.mark.parametrize(
    ("storage_status", "detail", "expected_handled_statuses"),
    [
        (404, "Task file not found: test.sh", []),
        (503, "Storage unavailable", [503]),
    ],
)
def test_selected_file_http_error_handling(
    client, storage_status, detail, expected_handled_statuses
):
    handled_statuses: list[int] = []

    async def record_http_exception(_request: Request, exc: HTTPException):
        handled_statuses.append(exc.status_code)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=exc.headers,
        )

    client.app.add_exception_handler(HTTPException, record_http_exception)

    @asynccontextmanager
    async def fake_get_read_session():
        yield object()

    resolve_source = AsyncMock(return_value=(3, "tasks/task-1/v3/"))
    get_file = AsyncMock(side_effect=HTTPException(storage_status, detail=detail))

    with (
        patch("api.routers.tasks.get_read_session", new=fake_get_read_session),
        patch("api.routers.tasks.resolve_task_file_source", new=resolve_source),
        patch("api.routers.tasks.get_task_file_content_s3", new=get_file),
    ):
        response = client.get("/tasks/task-1/files/test.sh?version=3")

    assert response.status_code == storage_status
    assert response.json() == {"detail": detail}
    assert handled_statuses == expected_handled_statuses

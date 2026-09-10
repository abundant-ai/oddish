"""GET /tasks/{id}/files request-shape contract."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from api.app import create_app
from oddish.core.task_files import TaskFileSource


@pytest.fixture
def client(monkeypatch):
    from auth import APIKeyScope, AuthContext, AuthMethod, get_auth_context

    fake_auth = AuthContext(
        method=AuthMethod.API_KEY,
        org_id="org-1",
        user_id="u-1",
        scope=APIKeyScope.READ,
    )

    async def fake_require_auth():
        return fake_auth

    app = create_app()
    app.dependency_overrides[get_auth_context] = fake_require_auth
    monkeypatch.setattr("auth.require_execution_org", AsyncMock(return_value=object()))
    return TestClient(app)


@pytest.mark.parametrize("version_query", ["", "version=3&"])
def test_tree_only_listing_forwards_inline_and_presign_flags(client, version_query):
    @asynccontextmanager
    async def fake_get_read_session():
        yield object()

    resolve_source = AsyncMock(
        return_value=TaskFileSource(
            3,
            "tasks/task-1/v3/",
            "tasks/task-1/v3-files/.oddish-manifest.json",
            "hash-v3",
        )
    )
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
        patch("auth.get_read_session", new=fake_get_read_session),
        patch("api.routers.tasks.resolve_task_file_source", new=resolve_source),
        patch("api.routers.tasks.list_task_files_s3", new=list_files),
    ):
        response = client.get(
            f"/tasks/task-1/files?recursive=1&{version_query}inline=false&presign=false"
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
        expanded=True,
        expanded_manifest_key="tasks/task-1/v3-files/.oddish-manifest.json",
        source_hash="hash-v3",
    )


def test_directory_page_forwards_prefix_limit_and_cursor(client):
    @asynccontextmanager
    async def fake_get_read_session():
        yield object()

    resolve_source = AsyncMock(
        return_value=TaskFileSource(
            3,
            "tasks/task-1/v3/",
            "tasks/task-1/v3-files/.oddish-manifest.json",
            "hash-v3",
        )
    )
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
        patch("auth.get_read_session", new=fake_get_read_session),
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
        expanded=True,
        expanded_manifest_key="tasks/task-1/v3-files/.oddish-manifest.json",
        source_hash="hash-v3",
    )


def test_selected_file_forwards_preview_limit(client):
    @asynccontextmanager
    async def fake_get_read_session():
        yield object()

    resolve_source = AsyncMock(
        return_value=TaskFileSource(
            3,
            "tasks/task-1/v3/",
            "tasks/task-1/v3-files/.oddish-manifest.json",
            "hash-v3",
        )
    )
    get_file = AsyncMock(
        return_value={
            "path": "large.txt",
            "content": "preview",
            "size": 2_000_000,
            "is_truncated": True,
        }
    )

    with (
        patch("auth.get_read_session", new=fake_get_read_session),
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
        expanded=True,
        expanded_manifest_key="tasks/task-1/v3-files/.oddish-manifest.json",
        source_hash="hash-v3",
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

    resolve_source = AsyncMock(
        return_value=TaskFileSource(
            3,
            "tasks/task-1/v3/",
            "tasks/task-1/v3-files/.oddish-manifest.json",
            "hash-v3",
        )
    )
    get_file = AsyncMock(side_effect=HTTPException(storage_status, detail=detail))

    with (
        patch("auth.get_read_session", new=fake_get_read_session),
        patch("api.routers.tasks.resolve_task_file_source", new=resolve_source),
        patch("api.routers.tasks.get_task_file_content_s3", new=get_file),
    ):
        response = client.get("/tasks/task-1/files/test.sh?version=3")

    assert response.status_code == storage_status
    assert response.json() == {"detail": detail}
    assert handled_statuses == expected_handled_statuses


def test_batch_uses_one_authorized_source_and_releases_session_before_storage(
    client, monkeypatch
):
    active = False
    sessions = 0

    @asynccontextmanager
    async def session():
        nonlocal active, sessions
        sessions += 1
        active = True
        yield object()
        active = False

    async def resolve(*_, **kwargs):
        assert active
        assert kwargs == {"task_id": "task-1", "org_id": "org-1", "version": 7}
        return TaskFileSource(7, "tasks/task-1/v7/", None, "hash-7")

    async def list_directories(**kwargs):
        assert not active
        assert kwargs["directories"] == ["", "tests"]
        assert kwargs["version"] == 7
        assert kwargs["limit"] == 100
        return {
            "task_id": "task-1",
            "directories": {"": {"cursor": "root-2"}, "tests": {"cursor": "tests-2"}},
        }

    from types import SimpleNamespace

    monkeypatch.setattr("auth.get_read_session", session)
    monkeypatch.setattr("api.routers.tasks.resolve_task_file_source", resolve)
    monkeypatch.setattr(
        "oddish.core.sharing.helpers.get_storage_client",
        lambda: SimpleNamespace(list_task_directories=list_directories),
    )
    response = client.get(
        "/tasks/task-1/files?version=7&directories=&directories=tests&limit=100&recursive=0&inline=0&presign=0"
    )
    assert response.status_code == 200, response.text
    assert response.json()["source_hash"] == "hash-7"
    assert response.json()["directories"]["tests"]["cursor"] == "tests-2"
    assert sessions == 1


@pytest.mark.parametrize(
    "extra",
    [
        "recursive=1",
        "inline=1",
        "presign=1",
        "stream=1",
        "cursor=page-2",
        "prefix=tests",
    ],
)
def test_batch_rejects_incompatible_listing_modes(client, monkeypatch, extra):
    @asynccontextmanager
    async def session():
        yield object()

    monkeypatch.setattr("auth.get_read_session", session)
    monkeypatch.setattr(
        "api.routers.tasks.resolve_task_file_source",
        AsyncMock(return_value=TaskFileSource(7, "tasks/task-1/v7/", None, "hash-7")),
    )
    storage = AsyncMock()
    monkeypatch.setattr("oddish.core.sharing.helpers.get_storage_client", storage)
    response = client.get(
        "/tasks/task-1/files?directories=&recursive=0&inline=0&presign=0&" + extra
    )
    assert response.status_code == 400, response.text
    storage.assert_not_called()


@pytest.mark.parametrize("visible", [False, True])
def test_public_directory_batch_keeps_share_token_and_version_scope(
    client, monkeypatch, visible
):
    @asynccontextmanager
    async def session():
        yield object()

    access = AsyncMock(return_value=object() if visible else None)
    resolve = AsyncMock(
        return_value=TaskFileSource(7, "tasks/task-1/v7/", None, "hash-7")
    )
    storage = AsyncMock(
        return_value={"directories": {"tests": {"files": [], "cursor": "page-2"}}}
    )
    monkeypatch.setattr("oddish.core.sharing.public.get_read_session", session)
    monkeypatch.setattr(
        "oddish.core.sharing.public.get_public_task_for_experiment", access
    )
    monkeypatch.setattr("oddish.core.sharing.public.resolve_task_file_source", resolve)
    monkeypatch.setattr("oddish.core.sharing.public.list_task_files_s3", storage)
    response = client.get(
        "/public/experiments/share-token/tasks/task-1/files?version=7&directories=&directories=tests&recursive=0&inline=0&presign=0&limit=100"
    )
    assert access.await_args.args[1:] == ("share-token", "task-1")
    if not visible:
        assert response.status_code == 404
        resolve.assert_not_awaited()
        storage.assert_not_awaited()
    else:
        assert response.status_code == 200, response.text
        assert resolve.await_args.kwargs == {"task_id": "task-1", "version": 7}
        assert storage.await_args.kwargs["directories"] == ["", "tests"]
        assert storage.await_args.kwargs["inline"] is False
        assert storage.await_args.kwargs["source_hash"] == "hash-7"
        assert response.json()["directories"]["tests"]["cursor"] == "page-2"

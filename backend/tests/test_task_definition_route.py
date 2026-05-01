"""Tests for GET /tasks/{task_id}/definition.

The route fetches the task source archive from S3 (where oddish already
stores it) and streams it to agent-sandbox-service. These tests stub the
storage client and the DB lookup so they don't need real S3 or Postgres.
"""

from __future__ import annotations

import io
import tarfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from api.app import create_app


def _make_archive(task_toml: str = "name = 'test'\nverifier_command = 'bash verifier/run.sh'\n") -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        data = task_toml.encode()
        info = tarfile.TarInfo(name="task.toml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


@pytest.fixture
def app_with_stub_auth():
    from auth import APIKeyScope, AuthContext, AuthMethod, require_auth

    fake_auth = AuthContext(
        method=AuthMethod.API_KEY,
        org_id="org-1",
        user_id="u-1",
        scope=APIKeyScope.READ,
    )

    async def _fake_require_auth():
        return fake_auth

    app = create_app()
    app.dependency_overrides[require_auth] = _fake_require_auth
    return app


@pytest.fixture
def client(app_with_stub_auth):
    return TestClient(app_with_stub_auth)


@pytest.fixture
def fake_task():
    return SimpleNamespace(id="task-1", name="my-task", org_id="org-1", task_path="x")


def test_streams_archive_from_s3(client, fake_task):
    archive = _make_archive(
        "name = 'irrelevant'\nverifier_command = 'python verifier/check.py'\n"
    )

    fake_storage = SimpleNamespace(
        _load_task_archive=AsyncMock(return_value=(archive, []))
    )

    with patch(
        "api.routers.tasks.select",
        side_effect=lambda *a, **kw: __import__("sqlalchemy").select(*a, **kw),
    ), patch(
        "api.routers.tasks.get_session"
    ) as gs, patch(
        "oddish.db.get_storage_client", return_value=fake_storage
    ):
        gs.return_value.__aenter__.return_value.execute = AsyncMock(
            return_value=SimpleNamespace(scalar_one_or_none=lambda: fake_task)
        )
        gs.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = client.get("/tasks/task-1/definition")

    assert resp.status_code == 200
    assert resp.content == archive
    assert resp.headers["X-Task-Name"] == "my-task"
    assert resp.headers["X-Task-Verifier-Command"] == "python verifier/check.py"


def test_returns_404_when_archive_missing(client, fake_task):
    fake_storage = SimpleNamespace(
        _load_task_archive=AsyncMock(side_effect=FileNotFoundError())
    )

    with patch(
        "api.routers.tasks.get_session"
    ) as gs, patch(
        "oddish.db.get_storage_client", return_value=fake_storage
    ):
        gs.return_value.__aenter__.return_value.execute = AsyncMock(
            return_value=SimpleNamespace(scalar_one_or_none=lambda: fake_task)
        )
        gs.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = client.get("/tasks/task-1/definition")

    assert resp.status_code == 404


def test_returns_403_on_org_mismatch(client):
    other_org_task = SimpleNamespace(
        id="task-1", name="my-task", org_id="other-org", task_path="x"
    )

    with patch("api.routers.tasks.get_session") as gs:
        gs.return_value.__aenter__.return_value.execute = AsyncMock(
            return_value=SimpleNamespace(scalar_one_or_none=lambda: other_org_task)
        )
        gs.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = client.get("/tasks/task-1/definition")

    assert resp.status_code == 403


def test_default_verifier_command_when_toml_lacks_field(client, fake_task):
    archive = _make_archive("name = 'whatever'\n")

    fake_storage = SimpleNamespace(
        _load_task_archive=AsyncMock(return_value=(archive, []))
    )

    with patch(
        "api.routers.tasks.get_session"
    ) as gs, patch(
        "oddish.db.get_storage_client", return_value=fake_storage
    ):
        gs.return_value.__aenter__.return_value.execute = AsyncMock(
            return_value=SimpleNamespace(scalar_one_or_none=lambda: fake_task)
        )
        gs.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = client.get("/tasks/task-1/definition")

    assert resp.status_code == 200
    assert resp.headers["X-Task-Verifier-Command"] == "bash verifier/run.sh"

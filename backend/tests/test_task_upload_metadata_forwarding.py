"""Route-level tests for POST /tasks/upload/init and /tasks/upload/complete
on the backend (cloud) router -- the router that actually runs on oddish.app.

Five identical bugs on this branch shared one shape: a field silently
dropped at an untraced layer, so uploads succeed and persist nothing, with
no error at all. The most recent (C1) was that neither router forwarded
``task_metadata``/``provenance`` to the core functions; fixed in commit
48620359 for both the OSS server (see
``oddish/tests/test_task_upload_routes.py``, same pattern) and this router.
That OSS test never touches this file, so it proved nothing about the router
that actually serves production. These tests drive the real route handlers
via the app used by ``api.app.create_app`` and assert on persisted rows,
not response bodies alone.

``uploader_user_id`` is wired ONLY on this router (the OSS server has no
auth) -- no other test anywhere covers it.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from api.app import create_app
from auth import AuthContext, AuthMethod, require_auth
from oddish.core import tasks as tasks_api
from oddish.db import get_session
from oddish.db.models import (
    MetadataSource,
    TaskModel,
    TaskUploadEventModel,
    TaskVersionModel,
)


class _FakeStorage:
    """No archive is really uploaded in these tests -- fake the S3 round trip
    so completion doesn't depend on a live bucket."""

    async def get_presigned_upload_url(self, *_args, **_kwargs) -> str:
        return "https://storage.example/upload"

    async def object_exists(self, *_args, **_kwargs) -> bool:
        return True


@pytest.fixture(autouse=True)
def _mock_storage(monkeypatch):
    monkeypatch.setattr(tasks_api, "get_storage_client", lambda: _FakeStorage())


def _metadata_payload(**overrides) -> dict:
    base = dict(
        description="Port CBACT01C.",
        category="ml-training",
        category_raw="ml_training",
        topic_tags=["compilers"],
        author_name="Ada",
        allow_internet=False,
        cpus=4,
        memory_mb=16384,
    )
    base.update(overrides)
    return base


def _provenance_payload(**overrides) -> dict:
    base = dict(
        source_repo="abundant-ai/harbor-lh",
        source_commit="a" * 40,
        source_ref="main",
        uploader_is_ci=True,
        ci_provider="github_actions",
    )
    base.update(overrides)
    return base


@pytest.fixture
def org_id() -> str:
    return f"org_taskup_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def user_id() -> str:
    return f"user_taskup_{uuid.uuid4().hex[:8]}"


@pytest_asyncio.fixture
async def client(org_id, user_id):
    """A real app with ``require_auth`` overridden to a fixed identity.

    Uses ``httpx.AsyncClient`` (the pattern established by
    test_org_usage_endpoint.py / test_quota_bumps.py) rather than the sync
    ``TestClient``: the latter runs the app in its own event loop, which
    deadlocks against a pooled asyncpg connection opened by this test's own
    ``@pytest.mark.asyncio`` loop when asserting on persisted rows.

    ``tasks.org_id`` / ``tasks.created_by_user_id`` carry no FK in the ORM
    metadata -- the cloud migration that adds those constraints layers them
    on top (see backend/tests/conftest.py's ``api_keys`` comment for the
    same pattern) -- so no ``organizations``/``users`` row is required to
    exercise these routes.
    """
    app = create_app()
    fake_auth = AuthContext(method=AuthMethod.CLERK_JWT, org_id=org_id, user_id=user_id)
    app.dependency_overrides[require_auth] = lambda: fake_auth
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as async_client:
            yield async_client
    finally:
        app.dependency_overrides.clear()


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_tasks(org_id):
    yield
    async with get_session() as session:
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.org_id == org_id)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_content_unchanged_reupload_persists_through_init_route(
    client, org_id, user_id
):
    """(a) POST /tasks/upload/init, content-unchanged re-upload.

    Also the only place ``uploader_user_id`` is covered: it's wired ONLY on
    this router (the OSS server has no auth to source it from).
    """
    init1 = await client.post(
        "/tasks/upload/init",
        json={
            "name": "route-noop",
            "content_hash": "hash-same",
            "task_metadata": _metadata_payload(),
            "provenance": _provenance_payload(),
        },
    )
    assert init1.status_code == 200, init1.text

    complete1 = await client.post(
        "/tasks/upload/complete",
        json={
            "task_id": init1.json()["task_id"],
            "name": "route-noop",
            "version": init1.json()["version"],
            "content_hash": "hash-same",
            "register_task": True,
            "user": "route-noop-uploader",
            "task_metadata": _metadata_payload(),
            "provenance": _provenance_payload(),
        },
    )
    assert complete1.status_code == 200, complete1.text
    task_id = complete1.json()["task_id"]

    init2 = await client.post(
        "/tasks/upload/init",
        json={
            "name": "route-noop",
            "content_hash": "hash-same",
            "task_metadata": _metadata_payload(description="Corrected description."),
            "provenance": _provenance_payload(source_repo="someone/laptop-copy"),
        },
    )
    assert init2.status_code == 200, init2.text
    assert init2.json()["content_unchanged"] is True

    async with get_session() as session:
        task = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
        events = (
            (
                await session.execute(
                    select(TaskUploadEventModel).where(
                        TaskUploadEventModel.task_id == task_id
                    )
                )
            )
            .scalars()
            .all()
        )

    assert task.description == "Corrected description."
    assert task.category == "ml-training"
    assert task.org_id == org_id

    noop_events = [
        e for e in events if e.task_version_id is None and e.created_version is False
    ]
    assert len(noop_events) == 1
    assert noop_events[0].source_repo == "someone/laptop-copy"
    assert noop_events[0].uploader_user_id == user_id


@pytest.mark.asyncio
async def test_new_task_persists_through_complete_route(client, org_id, user_id):
    """(b) POST /tasks/upload/complete, new-task branch (register=True)."""
    init = await client.post(
        "/tasks/upload/init",
        json={
            "name": "route-newtask",
            "content_hash": "hash-v1",
            "task_metadata": _metadata_payload(),
            "provenance": _provenance_payload(),
        },
    )
    assert init.status_code == 200, init.text
    init_body = init.json()

    complete = await client.post(
        "/tasks/upload/complete",
        json={
            "task_id": init_body["task_id"],
            "name": "route-newtask",
            "version": init_body["version"],
            "content_hash": "hash-v1",
            "register_task": True,
            "user": "route-newtask-uploader",
            "task_metadata": _metadata_payload(),
            "provenance": _provenance_payload(),
        },
    )
    assert complete.status_code == 200, complete.text
    body = complete.json()

    async with get_session() as session:
        task = (
            await session.execute(
                select(TaskModel).where(TaskModel.id == body["task_id"])
            )
        ).scalar_one()
        version = (
            await session.execute(
                select(TaskVersionModel).where(
                    TaskVersionModel.id == body["version_id"]
                )
            )
        ).scalar_one()
        events = (
            (
                await session.execute(
                    select(TaskUploadEventModel).where(
                        TaskUploadEventModel.task_id == body["task_id"]
                    )
                )
            )
            .scalars()
            .all()
        )

    assert task.description == "Port CBACT01C."
    assert task.category == "ml-training"
    assert task.category_raw == "ml_training"
    assert task.metadata_source == MetadataSource.CLIENT
    assert task.org_id == org_id
    assert task.created_by_user_id == user_id

    assert version.cpus == 4
    assert version.memory_mb == 16384
    assert version.allow_internet is False
    assert version.description_snapshot == "Port CBACT01C."
    assert version.category_snapshot == "ml-training"

    assert len(events) == 1
    assert events[0].created_version is True
    assert events[0].source_repo == "abundant-ai/harbor-lh"
    assert events[0].uploader_user_id == user_id

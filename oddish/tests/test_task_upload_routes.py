"""Route-level tests for /tasks/upload/init and /tasks/upload/complete.

Unit tests on ``initialize_task_upload``/``complete_task_upload`` (see
test_task_registry_ingest.py) call the core functions directly and pass even
when a router forgets to forward ``task_metadata``/``provenance`` -- that gap
let four identical bugs through. These tests go through the actual FastAPI
route (extracted from ``oddish.server.api`` onto a minimal test app, so no
lifespan/background workers spin up) and assert on persisted rows.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core import tasks as tasks_api
from oddish.db.models import (
    MetadataSource,
    TaskModel,
    TaskUploadEventModel,
    TaskVersionModel,
)
from oddish.server import api as full_api


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


_ROUTE_PATHS = {"/tasks/upload/init", "/tasks/upload/complete"}


@pytest.fixture
def client() -> TestClient:
    """A minimal FastAPI app carrying only the two upload routes.

    Reusing ``oddish.server.api`` directly would trigger its lifespan (DB
    init, connection-pool warmup, auto-started queue workers) on every test.
    Route objects are self-contained, so lifting just these two onto a bare
    app exercises the real route handlers without any of that.
    """
    app = FastAPI()
    for route in full_api.routes:
        if getattr(route, "path", None) in _ROUTE_PATHS:
            app.router.routes.append(route)
    return TestClient(app)


_TEST_TASK_NAMES = (
    "route-noop",
    "route-newtask",
    "route-newversion",
    "route-badvocab",
    "route-oversized-description",
    "route-out-of-range-cpus",
)


@pytest_asyncio.fixture(autouse=True)
async def _clean_route_tasks(session):
    """The routes commit through their own internal session, independent of
    the ``session`` fixture's rollback -- clear residue before AND after."""
    await session.execute(
        TaskModel.__table__.delete().where(TaskModel.name.in_(_TEST_TASK_NAMES))
    )
    await session.commit()
    yield
    await session.execute(
        TaskModel.__table__.delete().where(TaskModel.name.in_(_TEST_TASK_NAMES))
    )
    await session.commit()


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


@pytest.mark.asyncio
async def test_content_unchanged_reupload_persists_through_init_route(client, session):
    """(a) /tasks/upload/init, content-unchanged re-upload."""
    init1 = client.post(
        "/tasks/upload/init",
        json={
            "name": "route-noop",
            "content_hash": "hash-same",
            "task_metadata": _metadata_payload(),
            "provenance": _provenance_payload(),
        },
    )
    assert init1.status_code == 200, init1.text

    complete1 = client.post(
        "/tasks/upload/complete",
        json={
            "task_id": init1.json()["task_id"],
            "name": "route-noop",
            "version": init1.json()["version"],
            "content_hash": "hash-same",
            "register_task": True,
            "task_metadata": _metadata_payload(),
            "provenance": _provenance_payload(),
        },
    )
    assert complete1.status_code == 200, complete1.text
    task_id = complete1.json()["task_id"]

    init2 = client.post(
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

    task = (
        await session.execute(select(TaskModel).where(TaskModel.id == task_id))
    ).scalar_one()
    events = (
        await session.execute(
            select(TaskUploadEventModel).where(
                TaskUploadEventModel.task_id == task_id
            )
        )
    ).scalars().all()

    assert task.description == "Corrected description."
    assert task.category == "ml-training"

    noop_events = [
        e for e in events if e.task_version_id is None and e.created_version is False
    ]
    assert len(noop_events) == 1
    assert noop_events[0].source_repo == "someone/laptop-copy"


@pytest.mark.asyncio
async def test_new_task_persists_through_complete_route(client, session):
    """(b) /tasks/upload/complete, new-task branch."""
    init = client.post(
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

    complete = client.post(
        "/tasks/upload/complete",
        json={
            "task_id": init_body["task_id"],
            "name": "route-newtask",
            "version": init_body["version"],
            "content_hash": "hash-v1",
            "register_task": True,
            "task_metadata": _metadata_payload(),
            "provenance": _provenance_payload(),
        },
    )
    assert complete.status_code == 200, complete.text
    body = complete.json()

    task = (
        await session.execute(select(TaskModel).where(TaskModel.id == body["task_id"]))
    ).scalar_one()
    assert task.description == "Port CBACT01C."
    assert task.category == "ml-training"
    assert task.category_raw == "ml_training"
    assert task.metadata_source == MetadataSource.CLIENT

    version = (
        await session.execute(
            select(TaskVersionModel).where(TaskVersionModel.id == body["version_id"])
        )
    ).scalar_one()
    assert version.cpus == 4
    assert version.memory_mb == 16384
    assert version.allow_internet is False
    assert version.description_snapshot == "Port CBACT01C."
    assert version.category_snapshot == "ml-training"

    events = (
        await session.execute(
            select(TaskUploadEventModel).where(
                TaskUploadEventModel.task_id == body["task_id"]
            )
        )
    ).scalars().all()
    assert len(events) == 1
    assert events[0].created_version is True
    assert events[0].source_repo == "abundant-ai/harbor-lh"


@pytest.mark.asyncio
async def test_new_version_cut_persists_through_complete_route(client, session):
    """(c) /tasks/upload/complete, existing-task branch cutting a new version."""
    init1 = client.post(
        "/tasks/upload/init",
        json={
            "name": "route-newversion",
            "content_hash": "hash-v1",
            "task_metadata": _metadata_payload(),
            "provenance": _provenance_payload(),
        },
    ).json()
    complete1 = client.post(
        "/tasks/upload/complete",
        json={
            "task_id": init1["task_id"],
            "name": "route-newversion",
            "version": init1["version"],
            "content_hash": "hash-v1",
            "register_task": True,
            "task_metadata": _metadata_payload(),
            "provenance": _provenance_payload(),
        },
    ).json()
    v1_id = complete1["version_id"]
    v1_before = (
        await session.execute(select(TaskVersionModel).where(TaskVersionModel.id == v1_id))
    ).scalar_one()
    v1_snapshot_before = v1_before.description_snapshot

    changed_metadata = _metadata_payload(
        description="Rewritten for v2.", category="systems", cpus=8
    )
    init2 = client.post(
        "/tasks/upload/init",
        json={
            "name": "route-newversion",
            "content_hash": "hash-v2",
            "task_metadata": changed_metadata,
            "provenance": _provenance_payload(),
        },
    )
    assert init2.status_code == 200, init2.text
    init2_body = init2.json()
    assert init2_body["content_unchanged"] is False
    assert init2_body["version"] == 2

    complete2 = client.post(
        "/tasks/upload/complete",
        json={
            "task_id": init2_body["task_id"],
            "name": "route-newversion",
            "version": init2_body["version"],
            "content_hash": "hash-v2",
            "register_task": True,
            "task_metadata": changed_metadata,
            "provenance": _provenance_payload(),
        },
    )
    assert complete2.status_code == 200, complete2.text
    complete2_body = complete2.json()
    assert complete2_body["version"] == 2
    assert complete2_body["version_id"] != v1_id

    task = (
        await session.execute(
            select(TaskModel)
            .where(TaskModel.id == complete1["task_id"])
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert task.description == "Rewritten for v2."
    assert task.category == "systems"

    v2 = (
        await session.execute(
            select(TaskVersionModel).where(
                TaskVersionModel.id == complete2_body["version_id"]
            )
        )
    ).scalar_one()
    assert v2.description_snapshot == "Rewritten for v2."
    assert v2.category_snapshot == "systems"
    assert v2.cpus == 8

    await session.refresh(v1_before)
    assert v1_before.description_snapshot == v1_snapshot_before
    assert v1_before.category_snapshot == "ml-training"

    events = (
        await session.execute(
            select(TaskUploadEventModel)
            .where(TaskUploadEventModel.task_id == complete1["task_id"])
            .order_by(TaskUploadEventModel.created_at)
        )
    ).scalars().all()
    assert len(events) == 2
    assert events[1].created_version is True
    assert events[1].task_version_id == complete2_body["version_id"]


@pytest.mark.asyncio
async def test_malformed_vocabulary_does_not_500_upload(client, session):
    """C3: an over-long category and a profane topic tag must never 500.

    TaskMetadata bounds category/topic_tags at 128 chars at the wire, but the
    default tag policy's name_max_len is 64 and profanity is ENFORCEd -- both
    would previously raise inside rebuild_derived_tags with no try/except,
    rolling back the whole upload. Metadata is best-effort and must never
    fail an upload.
    """
    metadata = _metadata_payload(
        # 100 chars: within TaskMetadata's wire bound (128) but past the
        # default tag policy's name_max_len (64) -- exactly the gap C3 closes.
        category="x" * 100,
        topic_tags=["fuck"],
    )
    init = client.post(
        "/tasks/upload/init",
        json={
            "name": "route-badvocab",
            "content_hash": "hash-v1",
            "task_metadata": metadata,
            "provenance": _provenance_payload(),
        },
    )
    assert init.status_code == 200, init.text
    init_body = init.json()

    complete = client.post(
        "/tasks/upload/complete",
        json={
            "task_id": init_body["task_id"],
            "name": "route-badvocab",
            "version": init_body["version"],
            "content_hash": "hash-v1",
            "register_task": True,
            "task_metadata": metadata,
            "provenance": _provenance_payload(),
        },
    )
    assert complete.status_code == 200, complete.text

    task = (
        await session.execute(
            select(TaskModel).where(TaskModel.id == complete.json()["task_id"])
        )
    ).scalar_one()
    # Descriptive metadata still persists even though tag derivation failed.
    assert task.description == "Port CBACT01C."


@pytest.mark.asyncio
async def test_wire_level_oversized_description_rejected_by_route(client, session):
    """Spec Testing #2, Pydantic-bound case: a request whose task_metadata
    violates a Pydantic field bound (description over 8192 chars) must be
    rejected AT THE WIRE with 422, not silently stored with the bad field
    dropped. C3 above covers the policy-level case (passes Pydantic, fails
    tag policy); this is the wire-level twin that was never written."""
    response = client.post(
        "/tasks/upload/init",
        json={
            "name": "route-oversized-description",
            "content_hash": "hash-v1",
            "task_metadata": _metadata_payload(description="x" * 8193),
            "provenance": _provenance_payload(),
        },
    )
    assert response.status_code == 422

    task = (
        await session.execute(
            select(TaskModel).where(TaskModel.name == "route-oversized-description")
        )
    ).scalar_one_or_none()
    assert task is None


@pytest.mark.asyncio
async def test_wire_level_out_of_range_number_rejected_by_route(client, session):
    """Spec Testing #2, Pydantic-bound case: an out-of-range numeric field
    (cpus over the 1024 ceiling) must also be rejected at the wire."""
    response = client.post(
        "/tasks/upload/init",
        json={
            "name": "route-out-of-range-cpus",
            "content_hash": "hash-v1",
            "task_metadata": _metadata_payload(cpus=2000),
            "provenance": _provenance_payload(),
        },
    )
    assert response.status_code == 422

    task = (
        await session.execute(
            select(TaskModel).where(TaskModel.name == "route-out-of-range-cpus")
        )
    ).scalar_one_or_none()
    assert task is None

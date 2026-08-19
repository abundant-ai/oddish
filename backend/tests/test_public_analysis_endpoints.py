"""Public (share-token) analysis reads and durable generation status."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.services.summarize_trajectory import SCHEMA_VERSION
from oddish.db.models import WorkerJobStatus

TOKEN = "share-tok"
SUMMARY_URL = f"/public/experiments/{TOKEN}/trials/t-1/trajectory/summary"


@pytest.fixture
def client():
    return TestClient(create_app())


@pytest.fixture
def fake_session():
    session = MagicMock()
    session.execute = AsyncMock()
    return session


@pytest.fixture
def patched_session(fake_session):
    @asynccontextmanager
    async def _fake_get_session():
        yield fake_session

    return patch("api.routers.public_analysis.get_session", new=_fake_get_session)


# --------------------------------------------------------------------------
# Trajectory summary
# --------------------------------------------------------------------------


def test_summary_returns_stored_block(client, patched_session):
    summary = {"schema_version": "7", "summary": "ok", "highlights": []}
    with (
        patched_session,
        patch(
            "api.routers.public_analysis.get_public_trial_for_experiment",
            new=AsyncMock(
                return_value=SimpleNamespace(id="t-1", trajectory_summary=None)
            ),
        ),
        patch(
            "api.services.summarize_trajectory.load_stored_summary",
            new=AsyncMock(return_value=summary),
        ),
    ):
        resp = client.get(SUMMARY_URL)
    assert resp.status_code == 200
    assert resp.json() == summary


def test_summary_falls_back_to_the_trial_mirror(client, patched_session):
    """`preview_seed` copies trials but not `analyzer_blocks`, so a block-only
    read is empty on every preview deploy while the summary sits on the trial
    row."""
    mirror = {"schema_version": SCHEMA_VERSION, "summary": "from the mirror"}
    trial = SimpleNamespace(id="t-1", trajectory_summary=mirror)
    with (
        patched_session,
        patch(
            "api.routers.public_analysis.get_public_trial_for_experiment",
            new=AsyncMock(return_value=trial),
        ),
        patch(
            "api.services.summarize_trajectory._load_fresh_summary_block",
            new=AsyncMock(return_value=None),
        ),
    ):
        resp = client.get(SUMMARY_URL)
    assert resp.status_code == 200
    assert resp.json() == mirror


def test_stale_xai_mirror_queues_regeneration(client, fake_session, patched_session):
    """A truthy old summary must not prevent a fetchable xAI trial regenerating."""
    trial = SimpleNamespace(
        id="t-1",
        task_version_id="tv-1",
        trajectory_summary={"schema_version": "1", "summary": "old"},
        has_trajectory=False,
        agent="grok-build",
        finished_at=object(),
    )
    job = SimpleNamespace(
        id="job-stale", status=WorkerJobStatus.QUEUED, error_message=None
    )
    enqueue = AsyncMock(return_value=job)
    with (
        patched_session,
        patch(
            "api.routers.public_analysis.get_public_trial_for_experiment",
            new=AsyncMock(return_value=trial),
        ),
        patch(
            "api.services.summarize_trajectory._load_fresh_summary_block",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "api.services.summarize_trajectory.get_or_enqueue_summary_job",
            new=enqueue,
        ),
    ):
        resp = client.get(SUMMARY_URL)
    assert resp.status_code == 202
    assert resp.json()["job_id"] == "job-stale"
    enqueue.assert_awaited_once_with(fake_session, trial)


def test_stale_mirror_without_fetchable_trajectory_is_404(client, patched_session):
    trial = SimpleNamespace(
        id="t-1",
        trajectory_summary={"schema_version": "1", "summary": "old"},
        has_trajectory=False,
        agent="codex",
        finished_at=None,
    )
    with (
        patched_session,
        patch(
            "api.routers.public_analysis.get_public_trial_for_experiment",
            new=AsyncMock(return_value=trial),
        ),
        patch(
            "api.services.summarize_trajectory._load_fresh_summary_block",
            new=AsyncMock(return_value=None),
        ),
    ):
        resp = client.get(SUMMARY_URL)
    assert resp.status_code == 404


def test_summary_prefers_the_block_over_the_mirror(client, patched_session):
    trial = SimpleNamespace(
        id="t-1", trajectory_summary={"schema_version": SCHEMA_VERSION, "s": "mirror"}
    )
    block = {"schema_version": SCHEMA_VERSION, "s": "block"}
    with (
        patched_session,
        patch(
            "api.routers.public_analysis.get_public_trial_for_experiment",
            new=AsyncMock(return_value=trial),
        ),
        patch(
            "api.services.summarize_trajectory._load_fresh_summary_block",
            new=AsyncMock(return_value=block),
        ),
    ):
        resp = client.get(SUMMARY_URL)
    assert resp.json() == block


def test_summary_miss_enqueues_only_the_trial_summary_job(
    client, fake_session, patched_session
):
    trial = SimpleNamespace(
        id="t-1",
        trajectory_summary=None,
        has_trajectory=True,
        agent="codex",
        finished_at=object(),
    )
    job = SimpleNamespace(
        id="job-1", status=WorkerJobStatus.QUEUED, error_message=None
    )
    enqueue = AsyncMock(return_value=job)
    with (
        patched_session,
        patch(
            "api.routers.public_analysis.get_public_trial_for_experiment",
            new=AsyncMock(return_value=trial),
        ),
        patch(
            "api.services.summarize_trajectory.load_stored_summary",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "api.services.summarize_trajectory.get_or_enqueue_summary_job",
            new=enqueue,
        ),
    ):
        resp = client.get(SUMMARY_URL)
    assert resp.status_code == 202
    assert resp.headers["retry-after"] == "3"
    assert resp.json() == {
        "status": "queued",
        "job_id": "job-1",
        "retry_after_ms": 3000,
    }
    enqueue.assert_awaited_once_with(fake_session, trial)


def test_summary_failure_is_reported_without_reenqueue_loop(
    client, fake_session, patched_session
):
    trial = SimpleNamespace(
        id="t-1",
        trajectory_summary=None,
        has_trajectory=True,
        agent="codex",
        finished_at=object(),
    )
    job = SimpleNamespace(
        id="job-1",
        status=WorkerJobStatus.FAILED,
        error_message="provider rejected the request",
    )
    enqueue = AsyncMock(return_value=job)
    with (
        patched_session,
        patch(
            "api.routers.public_analysis.get_public_trial_for_experiment",
            new=AsyncMock(return_value=trial),
        ),
        patch(
            "api.services.summarize_trajectory.load_stored_summary",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "api.services.summarize_trajectory.get_or_enqueue_summary_job",
            new=enqueue,
        ),
    ):
        resp = client.get(SUMMARY_URL)
    assert resp.status_code == 502
    assert resp.json() == {"detail": "Summary generation failed"}
    enqueue.assert_awaited_once_with(fake_session, trial)


def test_summary_404_when_token_does_not_expose_the_trial(client, patched_session):
    """An unshared trial must not be readable, and must not be looked up."""
    load = AsyncMock()
    with (
        patched_session,
        patch(
            "api.routers.public_analysis.get_public_trial_for_experiment",
            new=AsyncMock(return_value=None),
        ),
        patch("api.services.summarize_trajectory.load_stored_summary", new=load),
    ):
        resp = client.get(SUMMARY_URL)
    assert resp.status_code == 404
    load.assert_not_awaited()

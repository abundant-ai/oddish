"""GET/POST /trials/{id}/trajectory/summary contract."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.services.summarize_trajectory import SCHEMA_VERSION
from oddish.db import WorkerJobStatus


@pytest.fixture
def app_with_stub_auth():
    from auth import APIKeyScope, AuthContext, AuthMethod, require_auth

    fake_auth = AuthContext(
        method=AuthMethod.API_KEY,
        org_id="org-1",
        user_id="viewer-1",
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
def fake_trial():
    return SimpleNamespace(
        id="t-1",
        org_id="org-1",
        name="trial-0",
        trial_s3_key="trials/t-1/",
        trajectory_summary=None,
        has_trajectory=True,
        agent="claude-code",
        finished_at=None,
    )


def _session_factory(*, latest_job=None):
    session = SimpleNamespace(scalar=AsyncMock(return_value=latest_job))

    @asynccontextmanager
    async def _get_session():
        yield session

    return _get_session


def _route_patches(fake_trial, *, latest_job=None):
    return (
        patch(
            "api.routers.trials.get_session",
            new=_session_factory(latest_job=latest_job),
        ),
        patch(
            "api.routers.trials.get_trial_for_org_core",
            new=AsyncMock(return_value=fake_trial),
        ),
    )


def test_get_returns_stored_summary(client, fake_trial):
    fake_trial.trajectory_summary = {
        "schema_version": SCHEMA_VERSION,
        "model": "claude-sonnet-4-6",
        "generated_at": "2026-05-02T00:00:00Z",
        "summary": "ok",
        "highlights": [],
    }
    get_session_patch, trial_patch = _route_patches(fake_trial)
    with get_session_patch, trial_patch:
        response = client.get("/trials/t-1/trajectory/summary")
    assert response.status_code == 200
    assert response.json() == fake_trial.trajectory_summary


def test_get_is_read_only_and_never_generates_or_enqueues(client, fake_trial):
    generator = AsyncMock()
    enqueue = AsyncMock()
    get_session_patch, trial_patch = _route_patches(fake_trial)
    with (
        get_session_patch,
        trial_patch,
        patch("api.services.summarize_trajectory.generate", new=generator),
        patch("api.routers.trials.enqueue_trajectory_summary_worker_job", new=enqueue),
    ):
        response = client.get("/trials/t-1/trajectory/summary")
    assert response.json() == {"status": "pending"}
    generator.assert_not_awaited()
    enqueue.assert_not_awaited()


def test_get_reports_exhausted_job_as_failed(client, fake_trial):
    get_session_patch, trial_patch = _route_patches(
        fake_trial, latest_job=WorkerJobStatus.FAILED
    )
    with get_session_patch, trial_patch:
        response = client.get("/trials/t-1/trajectory/summary")
    assert response.status_code == 200
    assert response.json()["status"] == "failed"


def test_post_queues_once_and_records_viewer(client, fake_trial):
    job = SimpleNamespace(id="summary-job-1")
    enqueue = AsyncMock(return_value=job)
    get_session_patch, trial_patch = _route_patches(fake_trial)
    with (
        get_session_patch,
        trial_patch,
        patch("api.routers.trials.enqueue_trajectory_summary_worker_job", new=enqueue),
    ):
        response = client.post("/trials/t-1/trajectory/summary")
    assert response.json() == {"status": "pending", "job_id": "summary-job-1"}
    enqueue.assert_awaited_once()
    assert enqueue.await_args.kwargs["triggered_by_user_id"] == "viewer-1"
    assert enqueue.await_args.kwargs["schema_version"] == SCHEMA_VERSION


def test_repeated_posts_return_the_same_active_job(client, fake_trial):
    job = SimpleNamespace(id="summary-job-1")
    enqueue = AsyncMock(return_value=job)
    get_session_patch, trial_patch = _route_patches(fake_trial)
    with (
        get_session_patch,
        trial_patch,
        patch("api.routers.trials.enqueue_trajectory_summary_worker_job", new=enqueue),
    ):
        responses = [client.post("/trials/t-1/trajectory/summary") for _ in range(2)]
    assert [response.json()["job_id"] for response in responses] == [
        "summary-job-1",
        "summary-job-1",
    ]


def test_post_after_failed_job_queues_a_retry(client, fake_trial):
    retry_job = SimpleNamespace(id="summary-job-2")
    enqueue = AsyncMock(return_value=retry_job)
    get_session_patch, trial_patch = _route_patches(fake_trial)
    with (
        get_session_patch,
        trial_patch,
        patch("api.routers.trials.enqueue_trajectory_summary_worker_job", new=enqueue),
    ):
        response = client.post("/trials/t-1/trajectory/summary")
    assert response.json()["job_id"] == "summary-job-2"


def test_get_and_post_return_404_without_trajectory(client, fake_trial):
    fake_trial.has_trajectory = False
    for method in (client.get, client.post):
        get_session_patch, trial_patch = _route_patches(fake_trial)
        with get_session_patch, trial_patch:
            response = method("/trials/t-1/trajectory/summary")
        assert response.status_code == 404

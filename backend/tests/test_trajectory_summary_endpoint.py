"""GET /trials/{id}/trajectory/summary: stored-summary reads, the 202
pending contract while a summarize trial is in flight, and the gated
``refresh=true`` enqueue."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from api.app import create_app


def _client(scope, created_by_role=None):
    from auth import AuthContext, AuthMethod, require_auth

    fake_auth = AuthContext(
        method=AuthMethod.API_KEY,
        org_id="org-1",
        user_id="u-1",
        scope=scope,
        api_key_created_by_role=created_by_role,
    )

    async def _fake_require_auth():
        return fake_auth

    app = create_app()
    app.dependency_overrides[require_auth] = _fake_require_auth
    return TestClient(app)


@pytest.fixture
def client():
    from auth import APIKeyScope

    return _client(APIKeyScope.READ)


@pytest.fixture
def tasks_client():
    from auth import APIKeyScope

    # Admin-minted: a member-created TASKS key is refused for refresh, the
    # same fail-closed rule as an analysis rerun.
    return _client(APIKeyScope.TASKS, created_by_role="admin")


def _trial(summary, *, kind="agent"):
    return SimpleNamespace(id="t-1", kind=kind, trajectory_summary=summary)


def _summarize_trial(status="queued"):
    return SimpleNamespace(id="t-1-9", status=SimpleNamespace(value=status))


@asynccontextmanager
async def _session():
    yield "session"


def test_endpoint_returns_stored_summary(client):
    summary = {"schema_version": 5, "components": []}
    with patch(
        "api.routers.trials._get_authorized_trial",
        new=AsyncMock(return_value=_trial(summary)),
    ):
        resp = client.get("/trials/t-1/trajectory/summary")
    assert resp.status_code == 200
    assert resp.json() == summary


def test_endpoint_404s_without_stored_summary(client):
    with (
        patch(
            "api.routers.trials._get_authorized_trial",
            new=AsyncMock(return_value=_trial(None)),
        ),
        patch(
            "api.routers.trials.find_live_summarize_trial",
            new=AsyncMock(return_value=None),
        ),
        patch("api.routers.trials.get_session", new=_session),
    ):
        resp = client.get("/trials/t-1/trajectory/summary")
    assert resp.status_code == 404


def test_a_live_summarize_trial_reports_pending_not_404(client):
    """The poller must keep waiting while the summary is on its way."""
    with (
        patch(
            "api.routers.trials._get_authorized_trial",
            new=AsyncMock(return_value=_trial(None)),
        ),
        patch(
            "api.routers.trials.find_live_summarize_trial",
            new=AsyncMock(return_value=_summarize_trial("running")),
        ),
        patch("api.routers.trials.get_session", new=_session),
    ):
        resp = client.get("/trials/t-1/trajectory/summary")
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "running"
    assert body["job_id"] == "t-1-9"
    assert body["retry_after_ms"] == 3000


def test_refresh_requires_more_than_read_scope(client):
    with patch(
        "api.routers.trials._get_authorized_trial",
        new=AsyncMock(return_value=_trial(None)),
    ):
        resp = client.get("/trials/t-1/trajectory/summary?refresh=true")
    assert resp.status_code == 403


def test_refresh_adopts_an_existing_run_and_answers_202(tasks_client):
    get_or_create = AsyncMock(return_value=_summarize_trial("running"))
    with (
        patch(
            "api.routers.trials._get_authorized_trial",
            new=AsyncMock(return_value=_trial({"stale": True})),
        ),
        patch("api.routers.trials.get_or_create_summarize_trial", new=get_or_create),
        patch("api.routers.trials.get_session", new=_session),
    ):
        resp = tasks_client.get("/trials/t-1/trajectory/summary?refresh=true")
    assert resp.status_code == 202
    assert resp.json() == {
        "status": "running",
        "job_id": "t-1-9",
        "retry_after_ms": 3000,
    }
    get_or_create.assert_awaited_once_with("session", target_trial_id="t-1")


def test_refresh_409s_for_a_non_agent_target(tasks_client):
    get_or_create = AsyncMock(return_value=None)
    with (
        patch(
            "api.routers.trials._get_authorized_trial",
            new=AsyncMock(return_value=_trial(None, kind="qa")),
        ),
        patch(
            "api.routers.trials.get_or_create_summarize_trial",
            new=get_or_create,
        ),
        patch("api.routers.trials.get_session", new=_session),
    ):
        resp = tasks_client.get("/trials/t-1/trajectory/summary?refresh=true")
    assert resp.status_code == 409
    assert "only agent trials" in resp.json()["detail"]
    get_or_create.assert_awaited_once_with("session", target_trial_id="t-1")

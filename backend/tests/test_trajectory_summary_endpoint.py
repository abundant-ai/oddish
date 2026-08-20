"""GET /trials/{id}/trajectory/summary: stored-summary reads, the 202
pending contract while a summarize trial is in flight, and the gated
``refresh=true`` enqueue."""

from __future__ import annotations

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


def _trial(summary):
    return SimpleNamespace(id="t-1", trajectory_summary=summary)


def _summarize_trial(status="queued"):
    return SimpleNamespace(id="t-1-9", status=SimpleNamespace(value=status))


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
            "api.routers.trials._live_summarize_trial",
            new=AsyncMock(return_value=None),
        ),
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
            "api.routers.trials._live_summarize_trial",
            new=AsyncMock(return_value=_summarize_trial("running")),
        ),
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


def test_refresh_enqueues_and_answers_202(tasks_client):
    request = AsyncMock(return_value=_summarize_trial("queued"))
    with (
        patch(
            "api.routers.trials._get_authorized_trial",
            new=AsyncMock(return_value=_trial({"stale": True})),
        ),
        patch("api.routers.trials._request_summarize_trial", new=request),
    ):
        resp = tasks_client.get("/trials/t-1/trajectory/summary?refresh=true")
    assert resp.status_code == 202
    assert resp.json()["status"] == "queued"
    request.assert_awaited_once_with("t-1")


def test_refresh_409s_when_the_trial_cannot_be_summarized(tasks_client):
    with (
        patch(
            "api.routers.trials._get_authorized_trial",
            new=AsyncMock(return_value=_trial(None)),
        ),
        patch(
            "api.routers.trials._request_summarize_trial",
            new=AsyncMock(return_value=None),
        ),
    ):
        resp = tasks_client.get("/trials/t-1/trajectory/summary?refresh=true")
    assert resp.status_code == 409

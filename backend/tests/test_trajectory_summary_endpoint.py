"""Authenticated trajectory-summary GET/POST lifecycle contract."""

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

    return _client(APIKeyScope.TASKS, created_by_role="admin")


@pytest.fixture
def member_tasks_client():
    from auth import APIKeyScope

    return _client(APIKeyScope.TASKS, created_by_role="member")


def _trial(summary, *, refresh_trial_id=None, kind="agent"):
    return SimpleNamespace(
        id="t-1",
        task_id="task-1",
        kind=kind,
        trajectory_summary=summary,
        trajectory_summary_refresh_trial_id=refresh_trial_id,
    )


def _summarize_trial(status="queued"):
    return SimpleNamespace(
        id="task-1-9",
        task_id="task-1",
        kind="summarize",
        status=SimpleNamespace(value=status),
        harbor_stage=None,
    )


@asynccontextmanager
async def _session(refresh_trial=None):
    yield SimpleNamespace(get=AsyncMock(return_value=refresh_trial))


def test_get_returns_stored_summary_when_no_refresh_is_current(client):
    summary = {"schema_version": 5, "components": []}
    with patch(
        "api.routers.trials._get_authorized_trial",
        new=AsyncMock(return_value=_trial(summary)),
    ):
        response = client.get("/trials/t-1/trajectory/summary")
    assert response.status_code == 200
    assert response.json() == {"summary": summary, "refresh": None}


def test_get_returns_missing_resource_when_nothing_is_published(client):
    with patch(
        "api.routers.trials._get_authorized_trial",
        new=AsyncMock(return_value=_trial(None)),
    ):
        response = client.get("/trials/t-1/trajectory/summary")
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("trial_status", "wire_status"),
    [("queued", "queued"), ("running", "running"), ("retrying", "retrying")],
)
def test_get_reports_current_live_refresh_before_stored_summary(
    client, trial_status, wire_status
):
    refresh = _summarize_trial(trial_status)
    with (
        patch(
            "api.routers.trials._get_authorized_trial",
            new=AsyncMock(
                return_value=_trial({"stale": True}, refresh_trial_id=refresh.id)
            ),
        ),
        patch("api.routers.trials.get_session", new=lambda: _session(refresh)),
    ):
        response = client.get("/trials/t-1/trajectory/summary")
    assert response.status_code == 200
    assert response.json() == {
        "summary": {"stale": True},
        "refresh": {
            "status": wire_status,
            "job_id": refresh.id,
            "retry_after_ms": 3000,
        },
    }


def test_get_reports_successful_unimported_refresh_as_settling(client):
    refresh = _summarize_trial("success")
    with (
        patch(
            "api.routers.trials._get_authorized_trial",
            new=AsyncMock(return_value=_trial(None, refresh_trial_id=refresh.id)),
        ),
        patch("api.routers.trials.get_session", new=lambda: _session(refresh)),
    ):
        response = client.get("/trials/t-1/trajectory/summary")
    assert response.status_code == 202
    assert response.json()["refresh"]["status"] == "settling"


def test_get_reports_current_failed_refresh(client):
    refresh = _summarize_trial("failed")
    with (
        patch(
            "api.routers.trials._get_authorized_trial",
            new=AsyncMock(
                return_value=_trial({"stale": True}, refresh_trial_id=refresh.id)
            ),
        ),
        patch("api.routers.trials.get_session", new=lambda: _session(refresh)),
    ):
        response = client.get("/trials/t-1/trajectory/summary")
    assert response.status_code == 200
    assert response.json() == {
        "summary": {"stale": True},
        "refresh": {
            "status": "failed",
            "job_id": refresh.id,
            "detail": (
                "Trajectory summary refresh failed; start a new refresh to retry"
            ),
        },
    }


def test_get_reports_cancelled_refresh_as_failed_even_if_trial_status_is_success(
    client,
):
    refresh = _summarize_trial("success")
    refresh.harbor_stage = "cancelled"
    with (
        patch(
            "api.routers.trials._get_authorized_trial",
            new=AsyncMock(return_value=_trial(None, refresh_trial_id=refresh.id)),
        ),
        patch("api.routers.trials.get_session", new=lambda: _session(refresh)),
    ):
        response = client.get("/trials/t-1/trajectory/summary")
    assert response.status_code == 409


def test_post_requires_tasks_scope(client):
    response = client.post("/trials/t-1/trajectory/summary")
    assert response.status_code == 403


def test_post_refuses_member_created_tasks_key(member_tasks_client):
    response = member_tasks_client.post("/trials/t-1/trajectory/summary")
    assert response.status_code == 403


def test_post_adopts_existing_refresh(tasks_client):
    refresh = _summarize_trial("running")
    get_or_create = AsyncMock(return_value=refresh)
    with (
        patch(
            "api.routers.trials._get_authorized_trial",
            new=AsyncMock(return_value=_trial({"stale": True})),
        ),
        patch("api.routers.trials.get_or_create_summarize_trial", new=get_or_create),
        patch("api.routers.trials.get_session", new=lambda: _session()),
    ):
        response = tasks_client.post("/trials/t-1/trajectory/summary")
    assert response.status_code == 200
    assert response.json() == {
        "summary": {"stale": True},
        "refresh": {
            "status": "running",
            "job_id": refresh.id,
            "retry_after_ms": 3000,
        },
    }
    session = get_or_create.await_args.args[0]
    get_or_create.assert_awaited_once_with(session, target_trial_id="t-1")


def test_post_refuses_ineligible_target(tasks_client):
    with (
        patch(
            "api.routers.trials._get_authorized_trial",
            new=AsyncMock(return_value=_trial(None, kind="qa")),
        ),
        patch(
            "api.routers.trials.get_or_create_summarize_trial",
            new=AsyncMock(return_value=None),
        ),
        patch("api.routers.trials.get_session", new=lambda: _session()),
    ):
        response = tasks_client.post("/trials/t-1/trajectory/summary")
    assert response.status_code == 409
    assert "only agent trials" in response.json()["detail"]


def test_trajectory_can_return_stored_summary_in_same_request(client):
    summary = {"schema_version": 5, "components": []}
    trajectory = {"schema_version": "1", "steps": []}
    with (
        patch(
            "api.routers.trials._get_authorized_trial",
            new=AsyncMock(return_value=_trial(summary)),
        ),
        patch(
            "api.routers.trials.read_trial_trajectory",
            new=AsyncMock(return_value=trajectory),
        ),
    ):
        response = client.get("/trials/t-1/trajectory?include_summary=1")

    assert response.status_code == 200
    assert response.json() == {
        "trajectory": trajectory,
        "summary_resource": {"summary": summary, "refresh": None},
    }


def test_trajectory_bundle_preserves_active_summary_refresh(client):
    refresh = _summarize_trial("running")
    trial = _trial({"stale": True}, refresh_trial_id=refresh.id)
    with (
        patch(
            "api.routers.trials._get_authorized_trial",
            new=AsyncMock(return_value=trial),
        ),
        patch(
            "api.routers.trials.read_trial_trajectory",
            new=AsyncMock(return_value={"steps": []}),
        ),
        patch("api.routers.trials.get_session", new=lambda: _session(refresh)),
    ):
        response = client.get("/trials/t-1/trajectory?include_summary=1")

    assert response.status_code == 200
    assert response.json()["summary_resource"] == {
        "summary": {"stale": True},
        "refresh": {
            "status": "running",
            "job_id": refresh.id,
            "retry_after_ms": 3000,
        },
    }

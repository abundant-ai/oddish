"""GET /trials/{id}/trajectory/summary endpoint (DB-backed)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.services.summarize_trajectory import (
    SCHEMA_VERSION,
    SummaryGenerationError,
)


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
def fake_trial():
    return SimpleNamespace(id="t-1", name="trial-0", trial_s3_key="trials/t-1/")


def _make_fake_session(fake_trial):
    """Return a context-manager mock for get_session() that yields a stub session."""
    mock_session = MagicMock()
    mock_session.get = AsyncMock(return_value=fake_trial)

    @asynccontextmanager
    async def _fake_get_session():
        yield mock_session

    return _fake_get_session


def test_endpoint_returns_summary_when_present(client, fake_trial):
    summary = {
        "schema_version": SCHEMA_VERSION,
        "model": "claude-sonnet-4-6",
        "generated_at": "2026-05-02T00:00:00Z",
        "summary": "ok",
        "highlights": [],
    }
    with patch(
        "api.routers.trials._get_authorized_trial",
        new=AsyncMock(return_value=fake_trial),
    ), patch(
        "api.routers.trials.get_session",
        new=_make_fake_session(fake_trial),
    ), patch(
        "api.routers.trials.get_or_generate_summary",
        new=AsyncMock(return_value=summary),
    ):
        resp = client.get("/trials/t-1/trajectory/summary")
    assert resp.status_code == 200
    assert resp.json() == summary


def test_endpoint_returns_404_when_no_trajectory(client, fake_trial):
    with patch(
        "api.routers.trials._get_authorized_trial",
        new=AsyncMock(return_value=fake_trial),
    ), patch(
        "api.routers.trials.get_session",
        new=_make_fake_session(fake_trial),
    ), patch(
        "api.routers.trials.get_or_generate_summary",
        new=AsyncMock(return_value=None),
    ):
        resp = client.get("/trials/t-1/trajectory/summary")
    assert resp.status_code == 404


def test_endpoint_returns_502_on_generation_error(client, fake_trial):
    async def _raise(_session, _trial, triggered_by_user_id=None, *, refresh=False):
        raise SummaryGenerationError("model returned garbage")

    with patch(
        "api.routers.trials._get_authorized_trial",
        new=AsyncMock(return_value=fake_trial),
    ), patch(
        "api.routers.trials.get_session",
        new=_make_fake_session(fake_trial),
    ), patch(
        "api.routers.trials.get_or_generate_summary", new=_raise
    ):
        resp = client.get("/trials/t-1/trajectory/summary")
    assert resp.status_code == 502
    assert "Summary generation failed" in resp.json()["detail"]


def test_refresh_needs_more_than_a_read_scope(client, fake_trial):
    """A read key may read a summary but not spend an LLM call replacing one."""
    spy = AsyncMock(return_value={"schema_version": "5"})
    with patch(
        "api.routers.trials._get_authorized_trial",
        new=AsyncMock(return_value=fake_trial),
    ), patch(
        "api.routers.trials.get_session",
        new=_make_fake_session(fake_trial),
    ), patch("api.routers.trials.get_or_generate_summary", new=spy):
        assert client.get("/trials/t-1/trajectory/summary").status_code == 200
        assert spy.await_args.kwargs["refresh"] is False

        resp = client.get("/trials/t-1/trajectory/summary?refresh=true")

    assert resp.status_code == 403
    # Rejected before any work is scheduled, not after paying for it.
    assert spy.await_count == 1


def test_endpoint_threads_refresh_through_to_the_service(fake_trial):
    """?refresh=true must reach get_or_generate_summary.

    It is the only way to replace a stored summary that is "fresh" by
    schema_version but stale in content, so a silently-dropped flag would
    hand the caller the very block it asked to be rid of.
    """
    from auth import APIKeyScope, AuthContext, AuthMethod, require_auth
    from models import UserRole

    async def _tasks_scoped_auth():
        return AuthContext(
            method=AuthMethod.API_KEY,
            org_id="org-1",
            user_id="u-1",
            scope=APIKeyScope.TASKS,
            # Member-created TASKS keys are barred, as they are for an
            # analysis rerun; this stands in for an admin-minted key.
            api_key_created_by_role=UserRole.ADMIN.value,
        )

    app = create_app()
    app.dependency_overrides[require_auth] = _tasks_scoped_auth
    spy = AsyncMock(return_value={"schema_version": "5"})
    with patch(
        "api.routers.trials._get_authorized_trial",
        new=AsyncMock(return_value=fake_trial),
    ), patch(
        "api.routers.trials.get_session",
        new=_make_fake_session(fake_trial),
    ), patch("api.routers.trials.get_or_generate_summary", new=spy):
        resp = TestClient(app).get("/trials/t-1/trajectory/summary?refresh=true")

    assert resp.status_code == 200
    assert spy.await_args.kwargs["refresh"] is True

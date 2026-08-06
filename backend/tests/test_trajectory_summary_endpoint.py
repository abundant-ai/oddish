"""GET /trials/{id}/trajectory/summary endpoint (DB-backed)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.services.summarize_trajectory import SCHEMA_VERSION


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
    return SimpleNamespace(
        id="t-1",
        name="trial-0",
        trial_s3_key="trials/t-1/",
        trajectory_summary=None,
        has_trajectory=True,
        agent="claude-code",
        finished_at=None,
    )


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
    ):
        fake_trial.trajectory_summary = summary
        resp = client.get("/trials/t-1/trajectory/summary")
    assert resp.status_code == 200
    assert resp.json() == summary


def test_endpoint_returns_pending_without_invoking_generator(client, fake_trial):
    generator = AsyncMock()
    with (
        patch(
            "api.routers.trials._get_authorized_trial",
            new=AsyncMock(return_value=fake_trial),
        ),
        patch(
            "api.services.summarize_trajectory.generate",
            new=generator,
        ),
    ):
        resp = client.get("/trials/t-1/trajectory/summary")
    assert resp.status_code == 200
    assert resp.json() == {"status": "pending"}
    generator.assert_not_awaited()


def test_endpoint_returns_pending_for_stale_summary(client, fake_trial):
    fake_trial.trajectory_summary = {
        "schema_version": "4",
        "summary": "stale",
    }
    with patch(
        "api.routers.trials._get_authorized_trial",
        new=AsyncMock(return_value=fake_trial),
    ):
        resp = client.get("/trials/t-1/trajectory/summary")
    assert resp.status_code == 200
    assert resp.json() == {"status": "pending"}


def test_endpoint_returns_404_when_no_trajectory(client, fake_trial):
    fake_trial.has_trajectory = False
    with patch(
        "api.routers.trials._get_authorized_trial",
        new=AsyncMock(return_value=fake_trial),
    ):
        resp = client.get("/trials/t-1/trajectory/summary")
    assert resp.status_code == 404

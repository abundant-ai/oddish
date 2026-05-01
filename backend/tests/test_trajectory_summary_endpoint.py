"""Endpoint test for GET /trials/{trial_id}/trajectory/summary.

We patch the lookups used inside the route so this is a pure router-shape
test (auth wiring + status codes + response body), not an integration test
of the lazy-generate flow.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from api.app import create_app


@pytest.fixture
def app_with_stub_auth():
    """Build the FastAPI app with `require_auth` overridden via dependency_overrides
    (the canonical FastAPI test pattern — monkeypatch can't intercept a Depends()
    reference that's already been captured at route registration time)."""
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


def test_endpoint_returns_summary_when_present(client, fake_trial):
    summary = {"schema_version": "1", "summary": "ok", "highlights": []}
    with patch(
        "api.routers.trials._get_authorized_trial",
        new=AsyncMock(return_value=fake_trial),
    ), patch(
        "api.routers.trials.read_trial_trajectory_summary",
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
        "api.routers.trials.read_trial_trajectory_summary",
        new=AsyncMock(return_value=None),
    ):
        resp = client.get("/trials/t-1/trajectory/summary")
    assert resp.status_code == 404


def test_endpoint_returns_502_on_generation_error(client, fake_trial):
    from api.services.summarize_trajectory import SummaryGenerationError

    async def _raise(_trial):
        raise SummaryGenerationError("model returned garbage")

    with patch(
        "api.routers.trials._get_authorized_trial",
        new=AsyncMock(return_value=fake_trial),
    ), patch(
        "api.routers.trials.read_trial_trajectory_summary", new=_raise
    ):
        resp = client.get("/trials/t-1/trajectory/summary")
    assert resp.status_code == 502

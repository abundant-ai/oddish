"""GET /trials/{id}/trajectory/summary returns the stored summary only."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from api.app import create_app


@pytest.fixture
def client():
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
    return TestClient(app)


def _trial(summary):
    return SimpleNamespace(trajectory_summary=summary)


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
    with patch(
        "api.routers.trials._get_authorized_trial",
        new=AsyncMock(return_value=_trial(None)),
    ):
        resp = client.get("/trials/t-1/trajectory/summary")
    assert resp.status_code == 404

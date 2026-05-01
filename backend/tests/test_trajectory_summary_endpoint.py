"""Endpoint test for GET /trials/{trial_id}/trajectory/summary.

The endpoint is a thin proxy to agent-sandbox-service. Self-hosters who
have not configured the service get a 404 (rendered as an empty summary
panel by the frontend).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from api.app import create_app


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
def fake_trial():
    return SimpleNamespace(id="t-1", name="trial-0", trial_s3_key="trials/t-1/")


def test_endpoint_proxies_to_service(app_with_stub_auth, fake_trial):
    summary = {"schema_version": "1", "summary": "from-service", "highlights": []}

    fake_client = AsyncMock()
    fake_client.get_trajectory_summary = AsyncMock(return_value=summary)

    app_with_stub_auth.state.agent_sandbox_client = fake_client
    client = TestClient(app_with_stub_auth)

    with patch(
        "api.routers.trials._get_authorized_trial",
        new=AsyncMock(return_value=fake_trial),
    ):
        resp = client.get("/trials/t-1/trajectory/summary")

    assert resp.status_code == 200
    assert resp.json() == summary
    fake_client.get_trajectory_summary.assert_awaited_once_with(
        trial_id="t-1", s3_prefix="trials/t-1/"
    )


def test_endpoint_returns_404_when_client_not_configured(app_with_stub_auth, fake_trial):
    if hasattr(app_with_stub_auth.state, "agent_sandbox_client"):
        delattr(app_with_stub_auth.state, "agent_sandbox_client")
    client = TestClient(app_with_stub_auth)

    with patch(
        "api.routers.trials._get_authorized_trial",
        new=AsyncMock(return_value=fake_trial),
    ):
        resp = client.get("/trials/t-1/trajectory/summary")

    assert resp.status_code == 404


def test_endpoint_propagates_service_error(app_with_stub_auth, fake_trial):
    fake_client = AsyncMock()
    fake_client.get_trajectory_summary = AsyncMock(side_effect=RuntimeError("boom"))

    app_with_stub_auth.state.agent_sandbox_client = fake_client
    client = TestClient(app_with_stub_auth)

    with patch(
        "api.routers.trials._get_authorized_trial",
        new=AsyncMock(return_value=fake_trial),
    ):
        with pytest.raises(RuntimeError):
            client.get("/trials/t-1/trajectory/summary")

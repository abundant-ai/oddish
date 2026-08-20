"""GET /experiments/{id}/share: single-round-trip share status.

The handler fetches the experiment row and its QA-report shadow id in one
self outer-join on a read-only (autocommit) session. These tests pin the
response shape for the three cases that matter: a primary experiment with
a shadow, a shadow experiment (which must never report a shadow of its
own), and a miss.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from auth import require_auth


def _auth_stub():
    return SimpleNamespace(org_id="org-1", require_scope=lambda scope: None)


@pytest.fixture
def app():
    app = create_app()
    app.dependency_overrides[require_auth] = _auth_stub
    try:
        yield app
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def client(app):
    return TestClient(app)


def _patched_read_session(row):
    """Patch the handler's read session so its one query returns ``row``."""
    result = MagicMock()
    result.first.return_value = row
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)

    @asynccontextmanager
    async def _fake_read_session():
        yield session

    return patch("api.routers.tasks.get_read_session", new=_fake_read_session)


def _experiment(**overrides):
    base = dict(
        name="exp-name",
        is_public=True,
        public_token="tok-123",
        description="a description",
        shadow_of=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_share_reports_the_joined_shadow_id(client):
    with _patched_read_session((_experiment(), "shadow-42")):
        resp = client.get("/experiments/e-1/share")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "exp-name"
    assert body["is_public"] is True
    assert body["public_token"] == "tok-123"
    assert body["shadow_of"] is None
    assert body["qa_report_experiment_id"] == "shadow-42"


def test_share_on_a_shadow_never_reports_a_shadow_of_its_own(client):
    """A shadow experiment answers with shadow_of set and no report id,
    even if the join produced a row artifact."""
    with _patched_read_session(
        (_experiment(shadow_of="primary-1"), "bogus-join-artifact")
    ):
        resp = client.get("/experiments/e-2/share")
    assert resp.status_code == 200
    body = resp.json()
    assert body["shadow_of"] == "primary-1"
    assert body["qa_report_experiment_id"] is None


def test_share_miss_is_a_404(client):
    with _patched_read_session(None):
        resp = client.get("/experiments/missing/share")
    assert resp.status_code == 404

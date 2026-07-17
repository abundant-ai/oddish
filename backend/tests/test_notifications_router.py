"""HTTP-level tests for /users/me/alert-preferences (no live Postgres).

Builds the real app but never touches the DB: `get_session` is a fake async
session, `read_alert_settings` is stubbed, and `require_auth` is overridden per
scenario.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import ProgrammingError

from api.app import create_app
from api.routers import notifications as notif_router
from auth import AuthContext, AuthMethod, require_auth
from slack_alert_settings import AlertSettings

pytestmark = pytest.mark.asyncio

PATH = "/users/me/alert-preferences"

VALID = {
    "cost_milestone_enabled": True,
    "expensive_trial_enabled": False,
    "experiment_failed_enabled": True,
    "trial_failed_enabled": False,
    "qa_failed_enabled": True,
    "experiment_milestone_usd": 2500,
    "trial_ping_usd": 50,
}

STUB_SETTINGS = AlertSettings(
    experiment_milestone_usd=1000.0, trial_ping_usd=200.0, is_override=False
)


class _FakeSession:
    def __init__(self, row=None):
        self.row = row
        self.statements: list[str] = []

    async def execute(self, statement):
        self.statements.append(str(statement))
        return SimpleNamespace(scalar_one_or_none=lambda: self.row)


class _UndefinedTableError(Exception):
    sqlstate = "42P01"


class _MissingTableSession:
    async def execute(self, statement):
        raise ProgrammingError("SELECT", {}, _UndefinedTableError("no such table"))


class _Ctx:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc):
        return False


def _install(monkeypatch, session):
    monkeypatch.setattr(notif_router, "get_session", lambda: _Ctx(session))

    async def _settings():
        return STUB_SETTINGS

    monkeypatch.setattr(notif_router, "read_alert_settings", _settings)


def _user_auth():
    return AuthContext(
        method=AuthMethod.CLERK_JWT,
        org_id="org_1",
        user_id="user_1",
        user_email="member@example.com",
    )


def _api_key_auth():
    return AuthContext(method=AuthMethod.API_KEY, org_id="org_1", user_id="key_1")


async def _call(method, *, auth=_user_auth, **kwargs):
    app = create_app()
    if auth is not None:
        app.dependency_overrides[require_auth] = auth
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, PATH, **kwargs)


async def test_get_returns_defaults_and_inherited_cutoffs(monkeypatch):
    _install(monkeypatch, _FakeSession(row=None))
    resp = await _call("GET")
    assert resp.status_code == 200
    body = resp.json()
    # No row -> all five DM types on, cutoffs unset (inheriting).
    assert body["cost_milestone_enabled"] is True
    assert body["experiment_milestone_usd"] is None
    assert body["trial_ping_usd"] is None
    # The inherited values report what an unset cutoff resolves to now.
    assert body["inherited_experiment_milestone_usd"] == 1000.0
    assert body["inherited_trial_ping_usd"] == 200.0


async def test_put_writes_and_echoes(monkeypatch):
    row = SimpleNamespace(
        cost_milestone_enabled=True,
        expensive_trial_enabled=False,
        experiment_failed_enabled=True,
        trial_failed_enabled=False,
        qa_failed_enabled=True,
        experiment_milestone_usd=2500,
        trial_ping_usd=50,
    )
    session = _FakeSession(row=row)
    _install(monkeypatch, session)
    resp = await _call("PUT", json=VALID)
    assert resp.status_code == 200
    body = resp.json()
    assert body["expensive_trial_enabled"] is False
    assert body["trial_ping_usd"] == 50
    assert any("INSERT INTO user_alert_preferences" in s for s in session.statements)
    assert any("ON CONFLICT" in s for s in session.statements)


@pytest.mark.parametrize(
    "field,value",
    [
        ("experiment_milestone_usd", 0),
        ("experiment_milestone_usd", -10),
        ("trial_ping_usd", 0),
        ("trial_ping_usd", -1),
    ],
)
async def test_put_rejects_nonpositive_cutoffs(monkeypatch, field, value):
    session = _FakeSession()
    _install(monkeypatch, session)
    resp = await _call("PUT", json={**VALID, field: value})
    assert resp.status_code == 422
    assert session.statements == []


async def test_null_cutoffs_are_accepted_as_inherit(monkeypatch):
    row = SimpleNamespace(
        cost_milestone_enabled=True,
        expensive_trial_enabled=True,
        experiment_failed_enabled=True,
        trial_failed_enabled=True,
        qa_failed_enabled=True,
        experiment_milestone_usd=None,
        trial_ping_usd=None,
    )
    _install(monkeypatch, _FakeSession(row=row))
    resp = await _call(
        "PUT",
        json={**VALID, "experiment_milestone_usd": None, "trial_ping_usd": None},
    )
    assert resp.status_code == 200
    assert resp.json()["experiment_milestone_usd"] is None


async def test_api_key_auth_is_rejected(monkeypatch):
    _install(monkeypatch, _FakeSession(row=None))
    resp = await _call("GET", auth=_api_key_auth)
    assert resp.status_code == 403


async def test_reads_survive_missing_table(monkeypatch):
    _install(monkeypatch, _MissingTableSession())
    resp = await _call("GET")
    assert resp.status_code == 503


async def test_requires_auth(monkeypatch):
    _install(monkeypatch, _FakeSession(row=None))
    resp = await _call("GET", auth=None)
    assert resp.status_code in (401, 403)

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import ProgrammingError

from api.app import create_app
from api.routers import admin as admin_router
from auth import require_admin
from slack_alert_settings import DEFAULT_ALERT_SETTINGS

VALID = {
    "experiment_milestone_usd": 2000,
    "experiment_repeat_usd": 500,
    "trial_ping_usd": 250,
    "trial_escalation_usd": 1500,
    "always_ping_emails": ["ops@example.com", "sre@example.com"],
}


class _Session:
    """Records statements; reports whichever override row it was seeded with."""

    def __init__(self, row=None):
        self.calls: list[str] = []
        self.row = row

    async def execute(self, statement):
        self.calls.append(str(statement))
        return SimpleNamespace(scalar_one_or_none=lambda: self.row)


class _UndefinedTableError(Exception):
    """Stands in for the driver's undefined-table error, which carries the
    SQLSTATE that pg_errors sniffs for."""

    sqlstate = "42P01"


class _UndefinedTableSession:
    async def execute(self, statement):
        raise ProgrammingError(
            "SELECT 1", {}, _UndefinedTableError("relation does not exist")
        )


@asynccontextmanager
async def _fake_session(session):
    yield session


def _app(admin: bool = True):
    app = create_app()
    if admin:
        app.dependency_overrides[require_admin] = lambda: SimpleNamespace(
            user_id="user-1"
        )
    return app


async def _call(app, method: str, **kwargs):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.request(method, "/admin/slack-alert-settings", **kwargs)


@pytest.mark.asyncio
async def test_get_returns_defaults_when_no_override_row(monkeypatch):
    session = _Session(row=None)
    monkeypatch.setattr(admin_router, "get_session", lambda: _fake_session(session))

    response = await _call(_app(), "GET")

    assert response.status_code == 200
    body = response.json()
    assert body["trial_ping_usd"] == DEFAULT_ALERT_SETTINGS.trial_ping_usd
    assert body["always_ping_emails"] == list(DEFAULT_ALERT_SETTINGS.always_ping_emails)
    # The pane labels defaults differently from a deliberate override.
    assert body["is_override"] is False


@pytest.mark.asyncio
async def test_put_writes_the_override_and_echoes_it_back(monkeypatch):
    row = SimpleNamespace(
        experiment_milestone_usd=2000,
        experiment_repeat_usd=500,
        trial_ping_usd=250,
        trial_escalation_usd=1500,
        always_ping_emails=["ops@example.com", "sre@example.com"],
    )
    session = _Session(row=row)
    monkeypatch.setattr(admin_router, "get_session", lambda: _fake_session(session))

    response = await _call(_app(), "PUT", json=VALID)

    assert response.status_code == 200
    assert response.json() == {**VALID, "is_override": True}
    assert "INSERT INTO slack_alert_settings" in session.calls[0]
    assert "ON CONFLICT" in session.calls[0]


@pytest.mark.asyncio
async def test_delete_drops_the_override_and_returns_defaults(monkeypatch):
    session = _Session()
    monkeypatch.setattr(admin_router, "get_session", lambda: _fake_session(session))

    response = await _call(_app(), "DELETE")

    assert response.status_code == 200
    assert response.json()["is_override"] is False
    assert "DELETE FROM slack_alert_settings" in session.calls[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field, value",
    [
        ("trial_ping_usd", 0),
        ("trial_ping_usd", -5),
        ("experiment_milestone_usd", 0),
        ("experiment_repeat_usd", -1),
        ("always_ping_emails", ["not-an-email"]),
    ],
)
async def test_put_rejects_nonsense(monkeypatch, field, value):
    session = _Session()
    monkeypatch.setattr(admin_router, "get_session", lambda: _fake_session(session))

    response = await _call(_app(), "PUT", json={**VALID, field: value})

    assert response.status_code == 422
    assert session.calls == []


@pytest.mark.asyncio
async def test_put_rejects_escalation_below_the_trial_floor(monkeypatch):
    """Below the floor, every alerting trial would escalate and ping the list."""
    session = _Session()
    monkeypatch.setattr(admin_router, "get_session", lambda: _fake_session(session))

    response = await _call(
        _app(),
        "PUT",
        json={**VALID, "trial_ping_usd": 500, "trial_escalation_usd": 100},
    )

    assert response.status_code == 422
    assert session.calls == []


@pytest.mark.asyncio
async def test_reads_survive_the_table_not_existing_yet(monkeypatch):
    """Deploy-before-migrate: a 503 beats a 500 while the schema catches up."""
    monkeypatch.setattr(
        admin_router, "get_session", lambda: _fake_session(_UndefinedTableSession())
    )

    response = await _call(_app(), "GET")

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_requires_admin():
    """No require_admin override: the dependency must reject the request."""
    response = await _call(_app(admin=False), "GET")

    assert response.status_code in (401, 403)

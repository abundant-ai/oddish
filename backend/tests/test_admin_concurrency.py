from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from api.app import create_app
from api.routers import admin as admin_router
from auth import require_admin
from oddish.config import settings


class _Session:
    def __init__(self, *, override_limit=None, audit_rows=()):
        self.calls = []
        self.override_limit = override_limit
        self.audit_rows = audit_rows

    async def execute(self, statement, params=None):
        sql = str(statement)
        params = params or {}
        self.calls.append((sql, params))
        old_limit = self.override_limit
        if "INSERT INTO model_concurrency_overrides" in sql:
            self.override_limit = params["concurrency_limit"]
        elif "DELETE FROM model_concurrency_overrides" in sql:
            self.override_limit = None
        rows = self.audit_rows if "FROM model_concurrency_audit" in sql else []
        if (
            "SELECT queue_key, concurrency_limit" in sql
            and self.override_limit is not None
        ):
            rows = [
                SimpleNamespace(
                    queue_key=params["queue_keys"][0],
                    concurrency_limit=self.override_limit,
                )
            ]
        return SimpleNamespace(
            all=lambda: rows,
            mappings=lambda: SimpleNamespace(
                all=lambda: [vars(row) for row in rows]
            ),
            scalar_one_or_none=lambda: old_limit,
        )


@asynccontextmanager
async def _fake_session(session):
    yield session


def _app():
    app = create_app()
    app.dependency_overrides[require_admin] = lambda: SimpleNamespace(
        org_id="org-1",
        user_id="user-1",
        api_key_id="key-1",
    )
    return app


def _app_without_attributed_user():
    app = create_app()
    app.dependency_overrides[require_admin] = lambda: SimpleNamespace(
        org_id="org-1",
        user_id=None,
        api_key_id="legacy-key",
    )
    return app


@pytest.fixture(autouse=True)
def operator_org(monkeypatch):
    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", "org-1")


@pytest.mark.asyncio
async def test_admin_update_normalizes_key_and_reports_both_limits(monkeypatch):
    session = _Session()
    monkeypatch.setattr(admin_router, "get_session", lambda: _fake_session(session))

    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.put(
            "/admin/concurrency",
            json={"queue_key": "MiniMax/MiniMax-M3", "limit": 96},
        )

    assert response.status_code == 200
    assert response.json() == {
        "queue_key": "minimax/minimax-m3",
        "limit": 96,
        "deploy_limit": settings.get_model_concurrency("minimax/minimax-m3"),
        "override_limit": 96,
    }
    statement, params = next(
        call
        for call in session.calls
        if "INSERT INTO model_concurrency_overrides" in call[0]
    )
    assert params == {"queue_key": "minimax/minimax-m3", "concurrency_limit": 96}
    audit_params = next(
        params
        for statement, params in session.calls
        if "INSERT INTO model_concurrency_audit" in statement
    )
    assert audit_params["actor_user_id"] == "user-1"
    assert audit_params["actor_api_key_id"] == "key-1"
    assert audit_params["old_override_limit"] is None
    assert audit_params["new_override_limit"] == 96


@pytest.mark.asyncio
async def test_admin_clearing_an_override_falls_back_to_the_deploy_limit(monkeypatch):
    session = _Session(override_limit=96)
    monkeypatch.setattr(admin_router, "get_session", lambda: _fake_session(session))
    deploy_limit = settings.get_model_concurrency("minimax/minimax-m3")

    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.put(
            "/admin/concurrency",
            json={"queue_key": "minimax/minimax-m3", "limit": None},
        )

    assert response.status_code == 200
    assert response.json() == {
        "queue_key": "minimax/minimax-m3",
        "limit": deploy_limit,
        "deploy_limit": deploy_limit,
        "override_limit": None,
    }
    assert any(
        "DELETE FROM model_concurrency_overrides" in call[0] for call in session.calls
    )


@pytest.mark.asyncio
async def test_admin_gets_current_concurrency_setting(monkeypatch):
    session = _Session(override_limit=96)
    monkeypatch.setattr(admin_router, "get_session", lambda: _fake_session(session))

    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.get(
            "/admin/concurrency", params={"queue_key": "MiniMax/MiniMax-M3"}
        )

    assert response.status_code == 200
    assert response.json()["queue_key"] == "minimax/minimax-m3"
    assert response.json()["limit"] == 96
    assert response.json()["override_limit"] == 96


@pytest.mark.asyncio
async def test_admin_lists_attributed_concurrency_audit(monkeypatch):
    changed_at = datetime(2026, 8, 5, tzinfo=timezone.utc)
    session = _Session(
        audit_rows=[
            SimpleNamespace(
                id=7,
                queue_key="anthropic/claude-sonnet-5",
                old_override_limit=8,
                new_override_limit=200,
                old_effective_limit=8,
                new_effective_limit=200,
                actor_user_id="user-1",
                actor_api_key_id="key-1",
                changed_at=changed_at,
            )
        ]
    )
    monkeypatch.setattr(admin_router, "get_session", lambda: _fake_session(session))

    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.get(
            "/admin/concurrency/audit",
            params={"queue_key": "anthropic/claude-sonnet-5", "limit": 10},
        )

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": 7,
            "queue_key": "anthropic/claude-sonnet-5",
            "old_override_limit": 8,
            "new_override_limit": 200,
            "old_effective_limit": 8,
            "new_effective_limit": 200,
            "actor_user_id": "user-1",
            "actor_api_key_id": "key-1",
            "changed_at": "2026-08-05T00:00:00Z",
        }
    ]


@pytest.mark.asyncio
async def test_admin_concurrency_rejects_invalid_limit():
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.put(
            "/admin/concurrency",
            json={"queue_key": "minimax/minimax-m3", "limit": 10_001},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_admin_concurrency_rejects_blank_queue_key():
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.put(
            "/admin/concurrency", json={"queue_key": "   ", "limit": 0}
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_admin_concurrency_rejects_a_misspelled_limit_field():
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.put(
            "/admin/concurrency",
            json={"queue_key": "minimax/minimax-m3", "limlt": 96},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_admin_concurrency_requires_admin():
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        response = await client.put(
            "/admin/concurrency",
            json={"queue_key": "minimax/minimax-m3", "limit": 96},
        )

    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_admin_concurrency_rejects_other_org(monkeypatch):
    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", "org-2")
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.put(
            "/admin/concurrency",
            json={"queue_key": "minimax/minimax-m3", "limit": 96},
        )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_admin_concurrency_rejects_unattributed_api_key():
    async with AsyncClient(
        transport=ASGITransport(app=_app_without_attributed_user()),
        base_url="http://test",
    ) as client:
        response = await client.put(
            "/admin/concurrency",
            json={"queue_key": "minimax/minimax-m3", "limit": 96},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "Attributed user required"}

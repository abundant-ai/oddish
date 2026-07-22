from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from api.app import create_app
from api.routers import admin as admin_router
from auth import require_admin
from oddish.config import settings


class _Session:
    def __init__(self):
        self.calls = []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))
        return SimpleNamespace(all=lambda: [])


@asynccontextmanager
async def _fake_session(session):
    yield session


def _app():
    app = create_app()
    app.dependency_overrides[require_admin] = lambda: object()
    return app


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
    statement, params = session.calls[0]
    assert "INSERT INTO model_concurrency_overrides" in statement
    assert params == {"queue_key": "minimax/minimax-m3", "concurrency_limit": 96}


@pytest.mark.asyncio
async def test_admin_clearing_an_override_falls_back_to_the_deploy_limit(monkeypatch):
    session = _Session()
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
    assert "DELETE FROM model_concurrency_overrides" in session.calls[0][0]


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

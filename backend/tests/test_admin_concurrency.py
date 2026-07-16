from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from api.app import create_app
from api.routers import admin as admin_router
from auth import require_admin
from oddish.core.admin import ModelConcurrencySetting


@pytest.mark.asyncio
async def test_admin_can_update_model_concurrency(monkeypatch):
    seen = {}

    @asynccontextmanager
    async def fake_session():
        yield object()

    async def fake_update(session, request):
        seen["request"] = request
        return ModelConcurrencySetting(
            queue_key="minimax/minimax-m3",
            limit=96,
            deploy_limit=128,
            override_limit=96,
        )

    monkeypatch.setattr(admin_router, "get_session", fake_session)
    monkeypatch.setattr(admin_router, "update_model_concurrency_core", fake_update)
    app = create_app()
    app.dependency_overrides[require_admin] = lambda: object()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(
            "/admin/concurrency",
            json={"queue_key": "MiniMax/MiniMax-M3", "limit": 96},
        )

    assert response.status_code == 200
    assert response.json() == {
        "queue_key": "minimax/minimax-m3",
        "limit": 96,
        "deploy_limit": 128,
        "override_limit": 96,
    }
    assert seen["request"].limit == 96


@pytest.mark.asyncio
async def test_admin_concurrency_rejects_invalid_limit():
    app = create_app()
    app.dependency_overrides[require_admin] = lambda: object()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(
            "/admin/concurrency",
            json={"queue_key": "minimax/minimax-m3", "limit": 10_001},
        )

    assert response.status_code == 422

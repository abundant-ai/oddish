import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from api.routers import load as load_router
from auth import AuthContext, AuthMethod, require_auth
from oddish.core.admin import LoadSnapshot, LoadTotals


def _snapshot() -> LoadSnapshot:
    return LoadSnapshot(
        submit_ceiling=64,
        pressure=0.0,
        ttl_seconds=5,
        totals=LoadTotals(queued=0, running=0),
        queues=[],
        timestamp="2026-06-22T00:00:00",
    )


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(load_router.router)
    return app


@pytest.mark.asyncio
async def test_load_requires_auth_returns_401_anonymous(monkeypatch):
    async def fake_snap():
        return _snapshot()

    monkeypatch.setattr(load_router, "get_cached_load_snapshot", fake_snap)
    transport = ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/load")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_load_authed_returns_snapshot(monkeypatch):
    async def fake_snap():
        return _snapshot()

    monkeypatch.setattr(load_router, "get_cached_load_snapshot", fake_snap)
    app = _app()
    app.dependency_overrides[require_auth] = lambda: AuthContext(
        method=AuthMethod.API_KEY, org_id="org_1"
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/load")
    assert resp.status_code == 200
    body = resp.json()
    assert body["submit_ceiling"] == 64
    assert body["pressure"] == 0.0
    assert body["ttl_seconds"] == 5
    assert body["totals"] == {"queued": 0, "running": 0}
    assert body["queues"] == []

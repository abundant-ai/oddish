import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from api import capacity_headers
from oddish.core.admin import LoadSnapshot, LoadTotals


def _snapshot(ceiling: int = 48, pressure: float = 0.25) -> LoadSnapshot:
    return LoadSnapshot(
        submit_ceiling=ceiling,
        pressure=pressure,
        ttl_seconds=5,
        totals=LoadTotals(queued=0, running=0),
        queues=[],
        timestamp="2026-06-22T00:00:00",
    )


def _app(monkeypatch) -> FastAPI:
    async def fake_snap():
        return _snapshot()

    monkeypatch.setattr(capacity_headers, "get_cached_load_snapshot", fake_snap)
    app = FastAPI()
    app.middleware("http")(capacity_headers.capacity_header_middleware)

    @app.post("/tasks/sweep")
    async def sweep():
        return {"ok": True}

    @app.get("/tasks/browse")
    async def browse():
        return {"ok": True}

    return app


@pytest.mark.asyncio
async def test_header_present_on_sweep_response(monkeypatch):
    transport = ASGITransport(app=_app(monkeypatch))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/tasks/sweep")
    assert resp.status_code == 200
    assert (
        resp.headers["Oddish-Submit-Concurrency"] == "ceiling=48; pressure=0.25; ttl=5"
    )
    assert resp.headers["RateLimit-Policy"] == "submit;q=48;qu=concurrent-requests;w=0"
    assert resp.headers["RateLimit"] == "submit;r=48;t=5"


@pytest.mark.asyncio
async def test_header_absent_on_non_submit_path(monkeypatch):
    transport = ASGITransport(app=_app(monkeypatch))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/tasks/browse")
    assert resp.status_code == 200
    assert "Oddish-Submit-Concurrency" not in resp.headers


@pytest.mark.asyncio
async def test_header_omitted_when_snapshot_fails(monkeypatch):
    async def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(capacity_headers, "get_cached_load_snapshot", boom)
    app = FastAPI()
    app.middleware("http")(capacity_headers.capacity_header_middleware)

    @app.post("/tasks/sweep")
    async def sweep():
        return {"ok": True}

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/tasks/sweep")
    assert resp.status_code == 200
    assert "Oddish-Submit-Concurrency" not in resp.headers

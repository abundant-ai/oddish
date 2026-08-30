from __future__ import annotations

from contextlib import asynccontextmanager
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from auth import require_admin
from auth.types import AuthContext, AuthMethod
from models import APIKeyScope

_ROUTER_PATH = (
    Path(__file__).resolve().parents[1] / "api" / "routers" / "deliveries.py"
)
_SPEC = spec_from_file_location("deliveries_route_under_test", _ROUTER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
deliveries = module_from_spec(_SPEC)
_SPEC.loader.exec_module(deliveries)


def test_delivery_routes_mounted() -> None:
    paths = {getattr(route, "path", None) for route in deliveries.router.routes}
    assert {
        "/deliveries",
        "/deliveries/{delivery_id}",
        "/deliveries/{delivery_id}/tasks",
        "/deliveries/{delivery_id}/tasks/{task_id}",
        "/deliveries/{delivery_id}/checks",
        "/deliveries/{delivery_id}/finalize",
        "/tasks/{task_id}/qa-history",
    } <= paths


def test_every_mutation_requires_admin() -> None:
    """POST/PATCH/PUT/DELETE delivery routes must depend on require_admin."""
    for route in deliveries.router.routes:
        methods = getattr(route, "methods", set()) or set()
        if not (methods - {"GET", "HEAD", "OPTIONS"}):
            continue
        dependant = getattr(route, "dependant", None)
        assert dependant is not None
        calls = [d.call for d in dependant.dependencies]
        assert require_admin in calls, (
            f"{route.path} {methods} is a mutation without require_admin"
        )


@pytest.mark.asyncio
async def test_create_delivery_passes_org_and_commits(monkeypatch) -> None:
    captured: dict = {}

    class FakeSession:
        committed = False

        async def commit(self):
            self.committed = True

    session = FakeSession()

    @asynccontextmanager
    async def fake_get_session():
        yield session

    class FakeDelivery:
        id = "d1"
        name = "batch"
        customer_name = None
        description = None
        status = "active"
        revision = 1
        is_public = False
        finalized_at = None
        from datetime import datetime, timezone

        created_at = datetime(2026, 8, 30, tzinfo=timezone.utc)
        updated_at = datetime(2026, 8, 30, tzinfo=timezone.utc)

    async def fake_create_core(_session, *, data, org_id, user_id):
        captured.update(data=data, org_id=org_id, user_id=user_id)
        return FakeDelivery()

    monkeypatch.setattr(deliveries, "get_session", fake_get_session)
    monkeypatch.setattr(deliveries, "create_delivery_core", fake_create_core)

    app = FastAPI()
    app.include_router(deliveries.router)
    app.dependency_overrides[require_admin] = lambda: AuthContext(
        method=AuthMethod.CLERK_JWT,
        org_id="org-1",
        user_id="user-1",
        scope=APIKeyScope.FULL,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/deliveries",
            json={"name": "batch", "task_ids": ["t1"]},
        )

    assert response.status_code == 200, response.text
    assert captured["org_id"] == "org-1"
    assert captured["user_id"] == "user-1"
    assert captured["data"].task_ids == ["t1"]
    assert session.committed is True
    assert response.json()["id"] == "d1"

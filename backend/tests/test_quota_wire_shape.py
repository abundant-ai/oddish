"""End-to-end 402 wire shape: with the custom exception handler deleted,
FastAPI's default rendering wraps QuotaExceeded's detail dict as
``{"detail": {...}}`` through a real route. The CLI reads both this and the
legacy flat shape (unit-tested in oddish/tests/test_sweep_batch_cli.py)."""

from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient

from api.app import create_app
from auth import require_auth
from auth.types import AuthContext, AuthMethod
from models import UserRole

DB_URL = os.environ.get("ODDISH_DATABASE_URL")
requires_db = pytest.mark.skipif(not DB_URL, reason="ODDISH_DATABASE_URL not set")


@requires_db
@pytest.mark.asyncio
async def test_sweep_route_renders_402_wrapped_in_detail(monkeypatch):
    from oddish.core.quota_admission import QuotaExceeded

    async def raise_quota_exceeded(*_args, **_kwargs):
        raise QuotaExceeded(3.0, 1.0, 2.0)

    import api.routers.tasks as tasks_router

    monkeypatch.setattr(
        tasks_router, "create_task_sweep_core", raise_quota_exceeded
    )

    app = create_app()
    app.dependency_overrides[require_auth] = lambda: AuthContext(
        method=AuthMethod.CLERK_JWT,
        org_id=f"org-wire-{os.getpid()}",
        user_role=UserRole.MEMBER,
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/tasks/sweep",
                json={
                    "task_id": "wire-shape-task",
                    "configs": [{"agent": "nop", "model": None, "n_trials": 1}],
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 402
    body = response.json()
    detail = body["detail"]  # FastAPI default wrap; no flat top-level message
    assert "message" not in body
    assert detail["used_usd"] == pytest.approx(3.0)
    assert detail["reserved_usd"] == pytest.approx(1.0)
    assert detail["limit_usd"] == pytest.approx(2.0)
    assert "Over your daily budget" in detail["message"]

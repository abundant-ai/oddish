"""Cancel route post-commit termination: the core returns raw remote handles;
the route terminates them via ``terminate_run_harvest`` after commit and never
leaks the raw keys into the response."""

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
async def test_cancel_route_terminates_harvest_after_commit(monkeypatch):
    import api.routers.tasks as tasks_router

    core_result = {
        "task_ids": ["t-1"],
        "not_found_task_ids": [],
        "tasks_found": 1,
        "tasks_cancelled": 1,
        "trials_cancelled": 2,
        "modal_function_call_ids": ["fc-1"],
        "worker_targets": [("daytona", "sb-1")],
    }
    terminated: list[dict] = []

    async def fake_cancel_core(session, task_ids, org_id=None, experiment_id=None):
        return core_result

    async def fake_terminate(result):
        # Runs after the route's session block (post-commit); pops raw keys.
        result.pop("modal_function_call_ids")
        result.pop("worker_targets")
        terminated.append(result)
        return 1

    monkeypatch.setattr(tasks_router, "cancel_tasks_runs", fake_cancel_core)
    monkeypatch.setattr(tasks_router, "terminate_run_harvest", fake_terminate)

    app = create_app()
    app.dependency_overrides[require_auth] = lambda: AuthContext(
        method=AuthMethod.CLERK_JWT,
        org_id=f"org-cxl-{os.getpid()}",
        user_role=UserRole.MEMBER,
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/tasks/cancel", json={"task_ids": ["t-1"]})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert terminated == [core_result]  # terminator invoked exactly once
    body = response.json()
    assert body["modal_calls_cancelled"] == 1
    assert "modal_function_call_ids" not in body
    assert "worker_targets" not in body
    assert body["trials_cancelled"] == 2

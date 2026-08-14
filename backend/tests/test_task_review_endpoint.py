"""Hosted task-review route is an auth-scoped adapter over shared core."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api.routers import tasks as tasks_router
from auth import APIKeyScope, require_auth
from oddish.analyze.models import ActionTier


def _auth():
    return SimpleNamespace(org_id="org-1", require_scope=Mock())


@pytest.mark.asyncio
async def test_hosted_review_forwards_org_and_typed_filters(monkeypatch):
    session = object()

    @asynccontextmanager
    async def fake_get_session():
        yield session

    core = AsyncMock(return_value="review-response")
    monkeypatch.setattr(tasks_router, "get_session", fake_get_session)
    monkeypatch.setattr(tasks_router, "get_task_review_core", core)
    auth = _auth()

    result = await tasks_router.get_task_review(
        task_id="task-name",
        auth=auth,
        version=18,
        experiment_id="exp-1",
        tier=[ActionTier.MUST_FIX, ActionTier.SHOULD_FIX],
        finding_limit=7,
        finding_cursor="finding-cursor",
        trial_limit=9,
        trial_cursor="trial-cursor",
    )

    assert result == "review-response"
    auth.require_scope.assert_called_once_with(APIKeyScope.READ)
    core.assert_awaited_once_with(
        session,
        task_ref="task-name",
        org_id="org-1",
        version=18,
        experiment_id="exp-1",
        tiers=[ActionTier.MUST_FIX, ActionTier.SHOULD_FIX],
        finding_limit=7,
        finding_cursor="finding-cursor",
        trial_limit=9,
        trial_cursor="trial-cursor",
    )


def test_hosted_review_route_is_registered_and_has_no_org_selector() -> None:
    route = next(
        route
        for route in tasks_router.router.routes
        if getattr(route, "path", None) == "/tasks/{task_id}/review"
    )

    assert route.methods == {"GET"}
    assert "org_id" not in {parameter.name for parameter in route.dependant.query_params}


def test_hosted_review_preserves_validation_and_not_found(monkeypatch):
    app = FastAPI()
    app.include_router(tasks_router.router)
    auth = _auth()
    app.dependency_overrides[require_auth] = lambda: auth

    invalid = TestClient(app).get("/tasks/task-1/review?tier=critical")
    assert invalid.status_code == 422

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    async def missing(*_args, **_kwargs):
        raise HTTPException(status_code=404, detail="Task task-1 not found")

    monkeypatch.setattr(tasks_router, "get_session", fake_get_session)
    monkeypatch.setattr(tasks_router, "get_task_review_core", missing)
    missing_response = TestClient(app).get("/tasks/task-1/review?tier=must_fix")

    assert missing_response.status_code == 404
    assert missing_response.json() == {"detail": "Task task-1 not found"}

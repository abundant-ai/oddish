"""Regression coverage for the hosted whole-task deletion endpoint."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from api.routers import tasks as tasks_router


def test_tasks_router_exposes_whole_task_delete():
    assert any(
        route.path == "/tasks/{task_id}" and "DELETE" in route.methods
        for route in tasks_router.router.routes
    )


@pytest.mark.asyncio
async def test_delete_task_commits_then_invalidates_and_terminates(monkeypatch):
    events: list[object] = []
    core_result = {
        "s3_prefixes": [],
        "deleted": {"task_id": "task-1"},
        "modal_function_call_ids": ["call-1"],
        "worker_targets": [("modal", "worker-1")],
    }

    class FakeSession:
        async def commit(self):
            events.append("commit")

    @asynccontextmanager
    async def fake_get_session():
        yield FakeSession()

    async def fake_delete_task_core(session, *, task_id, org_id):
        events.append(("core", session, task_id, org_id))
        return core_result

    def fake_invalidate_dashboard_cache(*, org_id):
        events.append(("invalidate", org_id))

    async def fake_terminate_run_harvest(result):
        events.append(("terminate", result))
        result.pop("modal_function_call_ids")
        result.pop("worker_targets")
        return 1

    monkeypatch.setattr(tasks_router, "get_session", fake_get_session)
    monkeypatch.setattr(tasks_router, "delete_task_core", fake_delete_task_core)
    monkeypatch.setattr(
        tasks_router, "invalidate_dashboard_cache", fake_invalidate_dashboard_cache
    )
    monkeypatch.setattr(
        tasks_router, "terminate_run_harvest", fake_terminate_run_harvest
    )

    auth = type("Auth", (), {"org_id": "org-1"})()
    result = await tasks_router.delete_task("task-1", auth)  # type: ignore[arg-type]

    assert events[0][0] == "core"
    assert events[0][2:] == ("task-1", "org-1")
    assert events[1:] == [
        "commit",
        ("invalidate", "org-1"),
        ("terminate", core_result),
    ]
    assert result == {
        "s3_prefixes": [],
        "deleted": {"task_id": "task-1"},
        "modal_calls_cancelled": 1,
    }

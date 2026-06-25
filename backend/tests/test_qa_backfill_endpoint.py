"""Lightweight wiring test: POST /tasks/{task_id}/qa/backfill.

Verifies that the route correctly passes body fields to ``backfill_task_analysis_core``
without hitting the database (core is monkeypatched).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from api.routers import tasks as tasks_router
from oddish.schemas import BackfillQARequest


@pytest.mark.asyncio
async def test_backfill_route_passes_body_to_core(monkeypatch):
    captured = {}

    async def fake_core(session, *, task_id, org_id, trial_ids, force, enable_analysis):
        captured.update(
            task_id=task_id,
            org_id=org_id,
            trial_ids=trial_ids,
            force=force,
            enable_analysis=enable_analysis,
        )
        return {"status": "queued", "task_id": task_id, "trial_count": 1, "reset_count": 1}

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    monkeypatch.setattr(tasks_router, "backfill_task_analysis_core", fake_core)
    monkeypatch.setattr(tasks_router, "get_session", fake_get_session)

    auth = type("Auth", (), {"org_id": "org-1", "require_scope": lambda self, s: None})()
    body = BackfillQARequest(force=True, enable_analysis=False, trial_ids=["tsk-1"])

    result = await tasks_router.backfill_task_qa("tsk", body, auth)  # type: ignore[arg-type]

    assert result["status"] == "queued"
    assert captured == {
        "task_id": "tsk",
        "org_id": "org-1",
        "trial_ids": ["tsk-1"],
        "force": True,
        "enable_analysis": False,
    }

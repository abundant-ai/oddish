"""Lightweight wiring tests for the QA trigger routes.

Each test verifies that a route passes its inputs to the right core
function. The core is monkeypatched, so no database is used.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from api.routers import tasks as tasks_router
from api.routers import trials as trials_router
from oddish.schemas import BackfillQARequest


def _fake_auth():
    return type(
        "Auth",
        (),
        {"org_id": "org-1", "require_scope": lambda self, *a, **k: None},
    )()


@asynccontextmanager
async def _fake_get_session():
    yield object()


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
        return {
            "status": "queued",
            "task_id": task_id,
            "trial_count": 1,
            "reset_count": 1,
        }

    monkeypatch.setattr(tasks_router, "backfill_task_analysis_core", fake_core)
    monkeypatch.setattr(tasks_router, "get_session", _fake_get_session)

    body = BackfillQARequest(force=True, enable_analysis=False, trial_ids=["tsk-1"])
    result = await tasks_router.backfill_task_qa("tsk", body, _fake_auth())  # type: ignore[arg-type]

    assert result["status"] == "queued"
    assert captured == {
        "task_id": "tsk",
        "org_id": "org-1",
        "trial_ids": ["tsk-1"],
        "force": True,
        "enable_analysis": False,
    }


@pytest.mark.asyncio
async def test_pre_trial_route_passes_task_to_core(monkeypatch):
    captured = {}

    async def fake_core(session, *, task_id, org_id):
        captured.update(task_id=task_id, org_id=org_id)
        return {"status": "queued", "task_id": task_id}

    monkeypatch.setattr(tasks_router, "rerun_pre_trial_audit_core", fake_core)
    monkeypatch.setattr(tasks_router, "get_session", _fake_get_session)

    result = await tasks_router.rerun_pre_trial_audit("tsk", _fake_auth())  # type: ignore[arg-type]

    assert result["status"] == "queued"
    assert captured == {"task_id": "tsk", "org_id": "org-1"}


@pytest.mark.asyncio
async def test_trial_analysis_route_passes_trial_to_core(monkeypatch):
    captured = {}

    async def fake_core(session, *, trial_id, org_id):
        captured.update(trial_id=trial_id, org_id=org_id)
        return {"status": "queued", "trial_id": trial_id}

    monkeypatch.setattr(trials_router, "rerun_trial_analysis_core", fake_core)
    monkeypatch.setattr(trials_router, "get_session", _fake_get_session)

    result = await trials_router.rerun_trial_analysis("tr-1", _fake_auth())  # type: ignore[arg-type]

    assert result["status"] == "queued"
    assert captured == {"trial_id": "tr-1", "org_id": "org-1"}

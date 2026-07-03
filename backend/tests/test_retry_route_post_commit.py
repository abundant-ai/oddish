"""Retry route post-commit termination: the core returns the superseded run's
raw remote handles (FC ids + sandbox worker targets); the route hands the WHOLE
result dict to ``terminate_run_harvest`` after commit (a bare list here
TypeError'd and leaked the container) and never leaks raw keys into the
response."""

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
async def test_retry_route_terminates_harvest_after_commit(monkeypatch):
    import oddish.core.helpers as core_helpers
    import api.routers.trials as trials_router

    core_result = {
        "status": "queued",
        "trial_id": "task-1-1",
        "superseded_trial_id": "task-1-0",
        "modal_function_call_ids": ["fc-retry-1"],
    }
    terminated: list[dict] = []

    async def fake_retry_core(session, *, trial_id, org_id=None, registry_auth=None):
        return core_result

    real_terminate = core_helpers.terminate_run_harvest

    async def recording_terminate(result):
        assert isinstance(result, dict)  # the W3 bug passed a bare list
        terminated.append(dict(result))
        result.pop("modal_function_call_ids", None)
        result.pop("worker_targets", None)
        return 1

    monkeypatch.setattr(trials_router, "retry_trial_core", fake_retry_core)
    monkeypatch.setattr(core_helpers, "terminate_run_harvest", recording_terminate)
    assert core_helpers.terminate_run_harvest is not real_terminate

    app = create_app()
    app.dependency_overrides[require_auth] = lambda: AuthContext(
        method=AuthMethod.CLERK_JWT,
        org_id=f"org-retry-{os.getpid()}",
        user_role=UserRole.MEMBER,
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/trials/task-1-0/retry")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert len(terminated) == 1  # terminator invoked exactly once, post-commit
    assert terminated[0]["modal_function_call_ids"] == ["fc-retry-1"]
    body = response.json()
    assert "modal_function_call_ids" not in body
    assert body["modal_calls_cancelled"] == 1
    assert body["trial_id"] == "task-1-1"
    assert body["superseded_trial_id"] == "task-1-0"


@requires_db
@pytest.mark.asyncio
async def test_retry_route_terminates_superseded_sandbox_target(monkeypatch):
    """End-to-end with the REAL core: the superseded trial's RUNNING worker_jobs
    row carries a non-Modal provider/external_id -- the route must terminate
    that sandbox exactly once post-commit and leak no raw keys."""
    import uuid

    import oddish.core.helpers as core_helpers
    import oddish.queue as queue_mod
    from oddish.db import (
        ExperimentModel,
        TaskModel,
        TaskStatus,
        TrialModel,
        TrialStatus,
        WorkerJobKind,
        WorkerJobModel,
        WorkerJobStatus,
        get_session,
    )

    suffix = uuid.uuid4().hex[:6]
    org_id = f"org-rt-{suffix}"
    experiment_id = f"exp-rt-{suffix}"
    task_id = f"task-rt-{suffix}"
    old_trial_id = f"{task_id}-0"

    async with get_session() as session:
        session.add(
            ExperimentModel(id=experiment_id, name=experiment_id, org_id=org_id)
        )
        session.add(
            TaskModel(
                id=task_id,
                name=task_id,
                org_id=org_id,
                user="tester",
                task_path="s3://test-bucket/retry-route-task",
                status=TaskStatus.RUNNING,
            )
        )
        session.add(
            TrialModel(
                id=old_trial_id,
                name=old_trial_id,
                task_id=task_id,
                experiment_id=experiment_id,
                org_id=org_id,
                agent="codex",
                provider="openai",
                queue_key="openai/gpt-5",
                model="gpt-5",
                is_probe=False,
                max_attempts=6,
                attempts=1,
                status=TrialStatus.FAILED,
                error_message="boom",
            )
        )
        session.add(
            WorkerJobModel(
                kind=WorkerJobKind.TRIAL,
                status=WorkerJobStatus.RUNNING,
                queue_key="openai/gpt-5",
                subject_table="trials",
                subject_id=old_trial_id,
                provider="daytona",
                external_id=f"sb-live-{suffix}",
            )
        )
        await session.commit()

    teardowns: list[tuple] = []

    async def recording_teardown(provider, external_id):
        teardowns.append((provider, external_id))
        return True

    async def fake_reserve(_session, *, task_id):
        return 1

    async def fake_enqueue(_session, **_kwargs):
        return None

    monkeypatch.setattr(core_helpers, "cancel_job_by_worker", recording_teardown)
    monkeypatch.setattr(queue_mod, "reserve_next_trial_index", fake_reserve)
    monkeypatch.setattr(queue_mod, "enqueue_trial_worker_job", fake_enqueue)

    app = create_app()
    app.dependency_overrides[require_auth] = lambda: AuthContext(
        method=AuthMethod.CLERK_JWT,
        org_id=org_id,
        user_role=UserRole.MEMBER,
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(f"/trials/{old_trial_id}/retry")
    finally:
        app.dependency_overrides.clear()
        async with get_session() as session:
            await session.execute(
                WorkerJobModel.__table__.delete().where(
                    WorkerJobModel.subject_id == old_trial_id
                )
            )
            await session.execute(
                TaskModel.__table__.delete().where(TaskModel.id == task_id)
            )
            await session.execute(
                ExperimentModel.__table__.delete().where(
                    ExperimentModel.id == experiment_id
                )
            )
            await session.commit()

    assert response.status_code == 200
    assert teardowns == [("daytona", f"sb-live-{suffix}")]  # exactly once
    body = response.json()
    assert "worker_targets" not in body
    assert "modal_function_call_ids" not in body
    assert body["superseded_trial_id"] == old_trial_id
    assert body["modal_calls_cancelled"] == 0  # no Modal FC on this job

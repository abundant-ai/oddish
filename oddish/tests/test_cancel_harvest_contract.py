"""Post-commit termination contract for the cancel core.

``cancel_tasks_runs`` must RETURN the harvested remote handles
(``modal_function_call_ids`` + ``worker_targets``) and never terminate them
in-transaction; ``terminate_run_harvest`` is the one post-commit terminator.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import oddish.core.helpers as core_helpers  # noqa: E402
from oddish.core.helpers import terminate_run_harvest  # noqa: E402
from oddish.db import (  # noqa: E402
    ExperimentModel,
    TaskModel,
    TaskStatus,
    TrialModel,
    TrialStatus,
    WorkerJobKind,
    WorkerJobModel,
    WorkerJobStatus,
)
from oddish.queue import cancel_tasks_runs  # noqa: E402


@pytest.mark.asyncio
async def test_cancel_core_returns_harvest_and_never_terminates_in_txn(
    monkeypatch, session
):
    calls: list[tuple] = []

    async def recording_teardown(provider, external_id):
        calls.append((provider, external_id))
        return True

    monkeypatch.setattr(core_helpers, "cancel_job_by_worker", recording_teardown)

    suffix = uuid.uuid4().hex[:6]
    org_id = f"org-cxl-{suffix}"
    experiment_id = f"exp-cxl-{suffix}"
    task_id = f"task-cxl-{suffix}"
    trial_id = f"{task_id}-0"
    session.add(ExperimentModel(id=experiment_id, name=experiment_id, org_id=org_id))
    session.add(
        TaskModel(
            id=task_id,
            name=task_id,
            org_id=org_id,
            user="tester",
            task_path="s3://test-bucket/cancel-harvest-task",
            status=TaskStatus.RUNNING,
        )
    )
    session.add(
        TrialModel(
            id=trial_id,
            name=trial_id,
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
            status=TrialStatus.RUNNING,
        )
    )
    session.add(
        WorkerJobModel(
            kind=WorkerJobKind.TRIAL,
            status=WorkerJobStatus.RUNNING,
            queue_key="openai/gpt-5",
            subject_table="trials",
            subject_id=trial_id,
            modal_function_call_id="fc-cxl-1",
            provider="daytona",
            external_id="sb-cxl-1",
        )
    )
    await session.commit()

    try:
        result = await cancel_tasks_runs(session, [task_id], org_id=org_id)

        assert result["modal_function_call_ids"] == ["fc-cxl-1"]
        assert result["worker_targets"] == [("daytona", "sb-cxl-1")]
        assert "sandboxes_terminated" not in result
        assert calls == []  # nothing terminated inside the transaction
    finally:
        await session.rollback()
        await session.execute(
            WorkerJobModel.__table__.delete().where(
                WorkerJobModel.subject_id == trial_id
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


@pytest.mark.asyncio
async def test_terminate_run_harvest_pops_keys_and_terminates_both(monkeypatch):
    import oddish.dispatch.backends.modal as modal_backend

    teardowns: list[tuple] = []
    fc_batches: list[list[str]] = []

    async def recording_teardown(provider, external_id):
        teardowns.append((provider, external_id))
        return True

    class _FakeDispatcher:
        name = "modal"

        async def cancel(self, handles):
            fc_batches.append([h.id for h in handles])
            return len(handles)

    monkeypatch.setattr(core_helpers, "cancel_job_by_worker", recording_teardown)
    monkeypatch.setattr(modal_backend, "ModalDispatcher", _FakeDispatcher)

    result = {
        "status": "cancelled",
        "modal_function_call_ids": ["fc-1", "fc-2"],
        "worker_targets": [("daytona", "sb-1")],
    }
    cancelled = await terminate_run_harvest(result)

    assert cancelled == 2
    assert fc_batches == [["fc-1", "fc-2"]]
    assert teardowns == [("daytona", "sb-1")]
    assert result == {"status": "cancelled"}  # raw handles never leak onward


@pytest.mark.asyncio
async def test_terminate_run_harvest_noop_on_empty(monkeypatch):
    async def must_not_run(*_a, **_k):
        raise AssertionError("no handles -> no termination calls")

    monkeypatch.setattr(core_helpers, "cancel_job_by_worker", must_not_run)
    assert await terminate_run_harvest({"status": "cancelled"}) == 0

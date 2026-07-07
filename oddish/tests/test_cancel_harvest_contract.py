"""Post-commit termination contract for the cancel core.

``cancel_tasks_runs`` must RETURN the harvested remote handles
(``modal_function_call_ids`` / ``graceful_modal_function_call_ids`` +
``worker_targets`` / ``deferred_worker_targets``) and never terminate them
in-transaction; ``terminate_run_harvest`` is the one post-commit terminator.
TRIAL workers are cancelled cooperatively (``force=False``) with their
sandbox teardown deferred to the cleanup sweep, so the in-flight Harbor run
can salvage partial artifacts; every other kind keeps the hard kill.
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
    trial_eph = f"{task_id}-1"
    trial_fcless = f"{task_id}-2"
    for tid in (trial_id, trial_eph, trial_fcless):
        session.add(
            TrialModel(
                id=tid,
                name=tid,
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
    # Modal TRIAL with a normal variant: the only one eligible for graceful
    # cooperative cancel + deferred sandbox teardown.
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
    # Ephemeral-variant TRIAL: Harbor runs in a subprocess that dies on
    # SIGKILL without salvaging, so it keeps the hard kill.
    session.add(
        WorkerJobModel(
            kind=WorkerJobKind.TRIAL,
            status=WorkerJobStatus.RUNNING,
            queue_key="openai/gpt-5",
            harbor_variant_id="ephemeral",
            subject_table="trials",
            subject_id=trial_eph,
            modal_function_call_id="fc-eph",
            provider="daytona",
            external_id="sb-eph",
        )
    )
    # fc-less TRIAL (OSS dispatcher / fc-lookup edge): no cooperative cancel
    # can ever reach it, so its sandbox must be torn down immediately.
    session.add(
        WorkerJobModel(
            kind=WorkerJobKind.TRIAL,
            status=WorkerJobStatus.RUNNING,
            queue_key="openai/gpt-5",
            subject_table="trials",
            subject_id=trial_fcless,
            modal_function_call_id=None,
            provider="daytona",
            external_id="sb-fcless",
        )
    )
    session.add(
        WorkerJobModel(
            kind=WorkerJobKind.QA,
            status=WorkerJobStatus.RUNNING,
            queue_key="openai/gpt-5",
            subject_table="tasks",
            subject_id=task_id,
            modal_function_call_id="fc-cxl-qa",
            provider="modal",
            external_id="sb-cxl-qa",
        )
    )
    await session.commit()

    try:
        result = await cancel_tasks_runs(session, [task_id], org_id=org_id)

        # Only the normal-variant Modal TRIAL goes graceful; ephemeral +
        # fc-less TRIALs and every non-TRIAL kind keep the hard-kill keys.
        assert result["graceful_modal_function_call_ids"] == ["fc-cxl-1"]
        assert result["deferred_worker_targets"] == [("daytona", "sb-cxl-1")]
        assert set(result["modal_function_call_ids"]) == {"fc-cxl-qa", "fc-eph"}
        assert result["worker_targets"] == sorted(
            [("modal", "sb-cxl-qa"), ("daytona", "sb-eph"), ("daytona", "sb-fcless")]
        )
        assert "sandboxes_terminated" not in result
        assert calls == []  # nothing terminated inside the transaction
    finally:
        await session.rollback()
        await session.execute(
            WorkerJobModel.__table__.delete().where(
                WorkerJobModel.subject_id.in_(
                    [trial_id, trial_eph, trial_fcless, task_id]
                )
            )
        )
        await session.execute(
            TrialModel.__table__.delete().where(TrialModel.task_id == task_id)
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
    fc_batches: list[tuple[list[str], bool]] = []

    async def recording_teardown(provider, external_id):
        teardowns.append((provider, external_id))
        return True

    class _FakeDispatcher:
        name = "modal"

        async def cancel(self, handles, *, force=True):
            fc_batches.append(([h.id for h in handles], force))
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
    assert fc_batches == [(["fc-1", "fc-2"], True)]
    assert teardowns == [("daytona", "sb-1")]
    assert result == {"status": "cancelled"}  # raw handles never leak onward


@pytest.mark.asyncio
async def test_terminate_run_harvest_graceful_trials_defer_sandboxes(monkeypatch):
    import oddish.dispatch.backends.modal as modal_backend

    teardowns: list[tuple] = []
    fc_batches: list[tuple[list[str], bool]] = []

    async def recording_teardown(provider, external_id):
        teardowns.append((provider, external_id))
        return True

    class _FakeDispatcher:
        name = "modal"

        async def cancel(self, handles, *, force=True):
            fc_batches.append(([h.id for h in handles], force))
            return len(handles)

    monkeypatch.setattr(core_helpers, "cancel_job_by_worker", recording_teardown)
    monkeypatch.setattr(modal_backend, "ModalDispatcher", _FakeDispatcher)

    result = {
        "status": "cancelled",
        "modal_function_call_ids": ["fc-qa"],
        "graceful_modal_function_call_ids": ["fc-trial-1", "fc-trial-2"],
        "worker_targets": [("modal", "sb-qa")],
        "deferred_worker_targets": [("daytona", "sb-trial-1")],
    }
    cancelled = await terminate_run_harvest(result)

    assert cancelled == 3
    # Hard kill for the QA worker, cooperative cancel for TRIAL workers.
    assert fc_batches == [
        (["fc-qa"], True),
        (["fc-trial-1", "fc-trial-2"], False),
    ]
    # Deferred TRIAL sandboxes are NOT torn down here (sweep + TTL backstop),
    # and no raw handles leak onward.
    assert teardowns == [("modal", "sb-qa")]
    assert result == {"status": "cancelled"}


@pytest.mark.asyncio
async def test_terminate_run_harvest_noop_on_empty(monkeypatch):
    async def must_not_run(*_a, **_k):
        raise AssertionError("no handles -> no termination calls")

    monkeypatch.setattr(core_helpers, "cancel_job_by_worker", must_not_run)
    assert await terminate_run_harvest({"status": "cancelled"}) == 0

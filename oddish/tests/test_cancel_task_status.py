"""Task-status semantics of cancellation.

Status reflects the execution outcome — CANCELLED means a person (or quota)
stopped the run, not that the pipeline broke — while the verdict columns
independently keep the judgment: a kept SUCCESS verdict + payload stays valid
on a cancelled task. A task cancelled in ANALYZING / VERDICT_PENDING already
finished every trial, so only QA was stopped and the run itself COMPLETED.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.quota_enforcement import (  # noqa: E402
    _ACTIVE_TASK_STATUSES as QUOTA_ACTIVE_TASK_STATUSES,
)
from oddish.db import (  # noqa: E402
    ExperimentModel,
    TaskModel,
    TaskStatus,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    WorkerJobKind,
    WorkerJobModel,
    WorkerJobStatus,
)
from oddish.queue import (  # noqa: E402
    ACTIVE_TASK_STATUSES,
    USER_CANCELLED_MESSAGE,
    cancel_tasks_runs,
)


def test_cancelled_is_terminal_not_active():
    assert TaskStatus.CANCELLED not in ACTIVE_TASK_STATUSES
    assert TaskStatus.CANCELLED not in QUOTA_ACTIVE_TASK_STATUSES


def _seed(session, *, suffix: str, task_status: TaskStatus, **task_kwargs):
    org_id = f"org-cst-{suffix}"
    experiment_id = f"exp-cst-{suffix}"
    task_id = f"task-cst-{suffix}"
    session.add(ExperimentModel(id=experiment_id, name=experiment_id, org_id=org_id))
    session.add(
        TaskModel(
            id=task_id,
            name=task_id,
            org_id=org_id,
            user="tester",
            task_path="s3://test-bucket/cancel-status-task",
            status=task_status,
            **task_kwargs,
        )
    )
    return org_id, experiment_id, task_id


async def _cleanup(session, *, experiment_id: str, task_id: str):
    await session.rollback()
    await session.execute(
        WorkerJobModel.__table__.delete().where(
            WorkerJobModel.subject_id.in_([task_id, f"{task_id}-0"])
        )
    )
    await session.execute(
        TrialModel.__table__.delete().where(TrialModel.task_id == task_id)
    )
    await session.execute(TaskModel.__table__.delete().where(TaskModel.id == task_id))
    await session.execute(
        ExperimentModel.__table__.delete().where(ExperimentModel.id == experiment_id)
    )
    await session.commit()


@pytest.mark.asyncio
async def test_cancel_running_task_marks_cancelled_and_keeps_verdict(session):
    """Cancelling an active run -> CANCELLED; a kept SUCCESS verdict stays."""
    suffix = uuid.uuid4().hex[:6]
    verdict_payload = {"verdict": "accept", "is_good": True}
    org_id, experiment_id, task_id = _seed(
        session,
        suffix=suffix,
        task_status=TaskStatus.RUNNING,
        verdict=verdict_payload,
        verdict_status=VerdictStatus.SUCCESS,
    )
    trial_id = f"{task_id}-0"
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
        )
    )
    await session.commit()

    try:
        result = await cancel_tasks_runs(session, [task_id], org_id=org_id)

        assert result["tasks_cancelled"] == 1
        task = await session.get(TaskModel, task_id)
        assert task.status == TaskStatus.CANCELLED
        assert task.finished_at is not None
        # Judgment is independent of execution: the kept verdict survives.
        assert task.verdict == verdict_payload
        assert task.verdict_status == VerdictStatus.SUCCESS
        trial = await session.get(TrialModel, trial_id)
        assert trial.status == TrialStatus.FAILED
        assert trial.error_message == USER_CANCELLED_MESSAGE
    finally:
        await _cleanup(session, experiment_id=experiment_id, task_id=task_id)


@pytest.mark.asyncio
async def test_cancel_verdict_pending_task_completes(session):
    """Trials all finished; cancelling only stops QA -> COMPLETED, not CANCELLED."""
    suffix = uuid.uuid4().hex[:6]
    org_id, experiment_id, task_id = _seed(
        session,
        suffix=suffix,
        task_status=TaskStatus.VERDICT_PENDING,
        verdict_status=VerdictStatus.RUNNING,
    )
    session.add(
        WorkerJobModel(
            kind=WorkerJobKind.QA,
            status=WorkerJobStatus.RUNNING,
            queue_key="qa",
            subject_table="tasks",
            subject_id=task_id,
        )
    )
    await session.commit()

    try:
        result = await cancel_tasks_runs(session, [task_id], org_id=org_id)

        assert result["tasks_cancelled"] == 1
        task = await session.get(TaskModel, task_id)
        assert task.status == TaskStatus.COMPLETED
        # No payload to keep -> the cancelled judgment reads FAILED + message.
        assert task.verdict_status == VerdictStatus.FAILED
        assert task.verdict_error == USER_CANCELLED_MESSAGE
    finally:
        await _cleanup(session, experiment_id=experiment_id, task_id=task_id)


@pytest.mark.asyncio
async def test_cancel_pending_task_without_trials_marks_cancelled(session):
    suffix = uuid.uuid4().hex[:6]
    org_id, experiment_id, task_id = _seed(
        session, suffix=suffix, task_status=TaskStatus.PENDING
    )
    await session.commit()

    try:
        result = await cancel_tasks_runs(session, [task_id], org_id=org_id)

        assert result["tasks_cancelled"] == 1
        task = await session.get(TaskModel, task_id)
        assert task.status == TaskStatus.CANCELLED
    finally:
        await _cleanup(session, experiment_id=experiment_id, task_id=task_id)

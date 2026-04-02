from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.db import (
    AnalysisStatus,
    TaskModel,
    TaskStatus,
    TrialModel,
    TrialStatus,
    VerdictStatus,
)
from oddish.queue import cancel_task_runs
from oddish.workers.queue import single_job


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        if isinstance(self._value, list):
            return self._value
        return []


class _FakeSession:
    def __init__(self, task, trials):
        self.task = task
        self.trials = trials

    async def execute(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is TaskModel:
            return _FakeScalarResult(self.task)
        if entity is TrialModel:
            return _FakeScalarResult(self.trials)
        raise AssertionError(f"Unexpected statement entity: {entity}")

    async def get(self, model, object_id):
        if model is TaskModel and object_id == self.task.id:
            return self.task
        return None

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_run_single_job_finalizes_claimed_job_when_worker_is_cancelled(monkeypatch):
    job = SimpleNamespace(
        id=123,
        entrypoint="openai/gpt-5.3-codex",
        payload=b"{}",
        queue_manager_id=uuid4(),
    )

    monkeypatch.setattr(single_job, "claim_single_job", AsyncMock(return_value=job))

    async def fake_heartbeat_claimed_job(*, job, stop_event):
        await stop_event.wait()

    async def fake_dispatch_claimed_job(**kwargs):
        raise asyncio.CancelledError("worker shutdown")

    finalize = AsyncMock(return_value=True)

    monkeypatch.setattr(single_job, "_heartbeat_claimed_job", fake_heartbeat_claimed_job)
    monkeypatch.setattr(single_job, "_dispatch_claimed_job", fake_dispatch_claimed_job)
    monkeypatch.setattr(single_job, "_finalize_claimed_job", finalize)

    with pytest.raises(asyncio.CancelledError, match="worker shutdown"):
        await single_job.run_single_job(
            "openai/gpt-5.3-codex",
            worker_id="worker-1",
            queue_slot=4,
        )

    finalize.assert_awaited_once()
    kwargs = finalize.await_args.kwargs
    assert kwargs["job"] is job
    assert kwargs["status"] == "canceled"
    assert kwargs["traceback_record"].exception_type == "CancelledError"


@pytest.mark.asyncio
async def test_cancel_task_runs_fails_active_analysis_and_verdict_state(monkeypatch):
    task = SimpleNamespace(
        id="task-1",
        status=TaskStatus.ANALYZING,
        finished_at=None,
        verdict_status=VerdictStatus.RUNNING,
        verdict_error=None,
        verdict_finished_at=None,
    )
    running_trial = SimpleNamespace(
        id="trial-running",
        task_id=task.id,
        status=TrialStatus.RUNNING,
        modal_function_call_id="fc-123",
        error_message=None,
        finished_at=None,
        harbor_stage=None,
        attempts=2,
        max_attempts=5,
        current_pgqueuer_job_id=42,
        current_worker_id="worker-1",
        current_queue_slot=3,
        analysis_status=AnalysisStatus.RUNNING,
        analysis_error=None,
        analysis_finished_at=None,
    )
    completed_trial = SimpleNamespace(
        id="trial-success",
        task_id=task.id,
        status=TrialStatus.SUCCESS,
        modal_function_call_id=None,
        error_message=None,
        finished_at=None,
        harbor_stage="completed",
        attempts=1,
        max_attempts=1,
        current_pgqueuer_job_id=None,
        current_worker_id=None,
        current_queue_slot=None,
        analysis_status=AnalysisStatus.RUNNING,
        analysis_error=None,
        analysis_finished_at=None,
    )
    session = _FakeSession(task=task, trials=[running_trial, completed_trial])

    monkeypatch.setattr(
        "oddish.queue.cancel_pgqueuer_jobs_for_trials",
        AsyncMock(return_value=2),
    )
    monkeypatch.setattr(
        "oddish.queue.cancel_pgqueuer_jobs_for_tasks",
        AsyncMock(return_value=1),
    )

    result = await cancel_task_runs(session, task.id)

    assert result["trials_cancelled"] == 1
    assert result["pgqueuer_jobs_cancelled"] == 3
    assert result["modal_function_call_ids"] == ["fc-123"]

    assert running_trial.status == TrialStatus.FAILED
    assert running_trial.error_message == "Cancelled by user"
    assert running_trial.harbor_stage == "cancelled"
    assert running_trial.max_attempts == running_trial.attempts
    assert running_trial.current_pgqueuer_job_id is None
    assert running_trial.current_worker_id is None
    assert running_trial.current_queue_slot is None
    assert running_trial.modal_function_call_id is None
    assert running_trial.analysis_status == AnalysisStatus.FAILED
    assert running_trial.analysis_error == "Cancelled by user"
    assert running_trial.analysis_finished_at is not None

    assert completed_trial.status == TrialStatus.SUCCESS
    assert completed_trial.analysis_status == AnalysisStatus.FAILED
    assert completed_trial.analysis_error == "Cancelled by user"
    assert completed_trial.analysis_finished_at is not None

    assert task.status == TaskStatus.FAILED
    assert task.finished_at is not None
    assert task.verdict_status == VerdictStatus.FAILED
    assert task.verdict_error == "Cancelled by user"
    assert task.verdict_finished_at is not None

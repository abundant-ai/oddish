from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from harbor.models.environment_type import EnvironmentType

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.db import (
    AnalysisStatus,
    Priority,
    TaskModel,
    TaskStatus,
    TrialModel,
    TrialStatus,
    VerdictStatus,
)
from oddish.queue import append_trials_to_task, cancel_task_runs, cancel_tasks_runs
from oddish.schemas import TaskSubmission, TrialSpec
from oddish.workers.queue import single_job
from oddish.workers.queue.single_job import ClaimedJob


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        if isinstance(self._value, list):
            return self._value[0] if self._value else None
        return self._value

    def scalars(self):
        return self

    def all(self):
        if isinstance(self._value, list):
            return self._value
        return []


class _FakeSession:
    def __init__(self, task=None, trials=None, tasks=None):
        if tasks is not None:
            self.tasks = list(tasks)
        elif task is not None:
            self.tasks = [task]
        else:
            self.tasks = []
        self.task = self.tasks[0] if self.tasks else None
        self.trials = list(trials or [])
        self.added = []
        self.refreshed = []

    async def execute(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is TaskModel:
            return _FakeScalarResult(self.tasks)
        if entity is TrialModel:
            return _FakeScalarResult(self.trials)
        raise AssertionError(f"Unexpected statement entity: {entity}")

    async def get(self, model, object_id):
        if model is TaskModel:
            for task in self.tasks:
                if task.id == object_id:
                    return task
        return None

    async def flush(self):
        return None

    def add(self, obj):
        self.added.append(obj)

    async def refresh(self, obj, attribute_names=None):
        self.refreshed.append((obj, attribute_names))
        return None


@pytest.mark.asyncio
async def test_run_single_job_propagates_cancellation(monkeypatch):
    """When a worker is cancelled, the CancelledError propagates upward."""
    job = ClaimedJob(
        job_type="trial",
        trial_id="trial-1",
        task_id="task-1",
        queue_key="openai/gpt-5.3-codex",
    )

    monkeypatch.setattr(single_job, "claim_single_job", AsyncMock(return_value=job))

    async def fake_dispatch(**kwargs):
        raise asyncio.CancelledError("worker shutdown")

    monkeypatch.setattr(single_job, "_dispatch_claimed_job", fake_dispatch)

    with pytest.raises(asyncio.CancelledError, match="worker shutdown"):
        await single_job.run_single_job(
            "openai/gpt-5.3-codex",
            worker_id="worker-1",
            queue_slot=4,
        )


@pytest.mark.asyncio
async def test_cancel_task_runs_fails_active_analysis_and_verdict_state(monkeypatch):
    task = SimpleNamespace(
        id="task-1",
        status=TaskStatus.ANALYZING,
        finished_at=None,
        verdict_status=VerdictStatus.RUNNING,
        verdict_error=None,
        verdict_finished_at=None,
        verdict_modal_function_call_id="fc-verdict",
    )
    running_trial = SimpleNamespace(
        id="trial-running",
        task_id=task.id,
        status=TrialStatus.RUNNING,
        modal_function_call_id="fc-123",
        analysis_modal_function_call_id="fc-analysis",
        error_message=None,
        finished_at=None,
        harbor_stage=None,
        attempts=2,
        max_attempts=5,
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
        analysis_modal_function_call_id=None,
        error_message=None,
        finished_at=None,
        harbor_stage="completed",
        attempts=1,
        max_attempts=1,
        current_worker_id=None,
        current_queue_slot=None,
        analysis_status=AnalysisStatus.RUNNING,
        analysis_error=None,
        analysis_finished_at=None,
    )
    session = _FakeSession(task=task, trials=[running_trial, completed_trial])

    result = await cancel_task_runs(session, task.id)

    assert result["trials_cancelled"] == 1
    assert result["modal_function_call_ids"] == [
        "fc-123",
        "fc-analysis",
        "fc-verdict",
    ]

    assert running_trial.status == TrialStatus.FAILED
    assert running_trial.error_message == "Cancelled by user"
    assert running_trial.harbor_stage == "cancelled"
    assert running_trial.max_attempts == running_trial.attempts
    assert running_trial.current_worker_id is None
    assert running_trial.current_queue_slot is None
    assert running_trial.modal_function_call_id is None
    assert running_trial.analysis_status == AnalysisStatus.FAILED
    assert running_trial.analysis_error == "Cancelled by user"
    assert running_trial.analysis_finished_at is not None
    assert running_trial.analysis_modal_function_call_id is None

    assert completed_trial.status == TrialStatus.SUCCESS
    assert completed_trial.analysis_status == AnalysisStatus.FAILED
    assert completed_trial.analysis_error == "Cancelled by user"
    assert completed_trial.analysis_finished_at is not None

    assert task.status == TaskStatus.FAILED
    assert task.finished_at is not None
    assert task.verdict_status == VerdictStatus.FAILED
    assert task.verdict_error == "Cancelled by user"
    assert task.verdict_finished_at is not None
    assert task.verdict_modal_function_call_id is None


@pytest.mark.asyncio
async def test_cancel_tasks_runs_batches_multiple_tasks_and_tracks_missing_ids():
    task_one = SimpleNamespace(
        id="task-1",
        status=TaskStatus.RUNNING,
        finished_at=None,
        verdict_status=None,
        verdict_error=None,
        verdict_finished_at=None,
        verdict_modal_function_call_id=None,
    )
    task_two = SimpleNamespace(
        id="task-2",
        status=TaskStatus.PENDING,
        finished_at=None,
        verdict_status=VerdictStatus.RUNNING,
        verdict_error=None,
        verdict_finished_at=None,
        verdict_modal_function_call_id="fc-verdict-2",
    )
    running_trial = SimpleNamespace(
        id="trial-running",
        task_id=task_one.id,
        status=TrialStatus.RUNNING,
        modal_function_call_id="fc-1",
        analysis_modal_function_call_id=None,
        error_message=None,
        finished_at=None,
        harbor_stage=None,
        attempts=3,
        max_attempts=5,
        current_worker_id="worker-1",
        current_queue_slot=1,
        analysis_status=None,
        analysis_error=None,
        analysis_finished_at=None,
    )
    queued_trial = SimpleNamespace(
        id="trial-queued",
        task_id=task_two.id,
        status=TrialStatus.QUEUED,
        modal_function_call_id=None,
        analysis_modal_function_call_id="fc-analysis-2",
        error_message=None,
        finished_at=None,
        harbor_stage=None,
        attempts=0,
        max_attempts=5,
        current_worker_id=None,
        current_queue_slot=None,
        analysis_status=AnalysisStatus.RUNNING,
        analysis_error=None,
        analysis_finished_at=None,
    )
    session = _FakeSession(
        tasks=[task_one, task_two],
        trials=[running_trial, queued_trial],
    )

    result = await cancel_tasks_runs(session, [task_one.id, task_two.id, "missing"])

    assert result["task_ids"] == [task_one.id, task_two.id]
    assert result["not_found_task_ids"] == ["missing"]
    assert result["tasks_found"] == 2
    assert result["tasks_cancelled"] == 2
    assert result["trials_cancelled"] == 2
    assert result["modal_function_call_ids"] == [
        "fc-1",
        "fc-analysis-2",
        "fc-verdict-2",
    ]

    assert running_trial.status == TrialStatus.FAILED
    assert running_trial.error_message == "Cancelled by user"
    assert running_trial.harbor_stage == "cancelled"
    assert running_trial.modal_function_call_id is None

    assert queued_trial.status == TrialStatus.FAILED
    assert queued_trial.analysis_status == AnalysisStatus.FAILED
    assert queued_trial.analysis_error == "Cancelled by user"
    assert queued_trial.analysis_modal_function_call_id is None

    assert task_one.status == TaskStatus.FAILED
    assert task_two.status == TaskStatus.FAILED
    assert task_two.verdict_status == VerdictStatus.FAILED
    assert task_two.verdict_error == "Cancelled by user"
    assert task_two.verdict_modal_function_call_id is None


@pytest.mark.asyncio
async def test_append_trials_to_task_resets_completed_task_state():
    task = SimpleNamespace(
        id="task-1",
        name="demo-task",
        org_id="org-1",
        task_path="/tmp/demo-task",
        user="rishi",
        priority=Priority.LOW,
        tags={"source": "test"},
        run_analysis=True,
        status=TaskStatus.COMPLETED,
        finished_at="done",
        verdict={"classification": "success"},
        verdict_status=VerdictStatus.SUCCESS,
        verdict_error=None,
        verdict_started_at="started",
        verdict_finished_at="finished",
    )
    existing_trials = [
        SimpleNamespace(
            id="task-1-0",
            task_id=task.id,
            agent="codex",
            model="openai/gpt-5.2-codex",
        ),
        SimpleNamespace(
            id="task-1-1",
            task_id=task.id,
            agent="oracle",
            model="default",
        ),
    ]
    session = _FakeSession(task=task, trials=existing_trials)
    submission = TaskSubmission(
        task_path=task.task_path,
        name=task.name,
        trials=[
            TrialSpec(
                agent="gemini-cli",
                model="google/gemini-3.1-pro-preview",
                environment=EnvironmentType.MODAL,
            ),
            TrialSpec(
                agent="claude-code",
                model="anthropic/claude-sonnet-4-6",
                environment=EnvironmentType.MODAL,
            ),
        ],
        user=task.user,
        priority=task.priority,
        experiment_id="exp-1",
        tags=task.tags,
        run_analysis=task.run_analysis,
        harbor={},
    )

    new_trials = await append_trials_to_task(session, task=task, submission=submission)

    assert len(new_trials) == 2
    assert [trial.id for trial in session.added] == ["task-1-2", "task-1-3"]
    assert [trial.name for trial in session.added] == ["demo-task-2", "demo-task-3"]
    assert session.added[0].queue_key == "google/gemini-3.1-pro-preview"
    assert session.added[1].queue_key == "anthropic/claude-sonnet-4-6"
    assert all(trial.status == TrialStatus.QUEUED for trial in session.added)

    assert task.status == TaskStatus.RUNNING
    assert task.finished_at is None
    assert task.verdict is None
    assert task.verdict_status is None
    assert task.verdict_error is None
    assert task.verdict_started_at is None
    assert task.verdict_finished_at is None
    assert session.refreshed == [(task, ["trials"])]

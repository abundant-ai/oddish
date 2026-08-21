from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from oddish.core.endpoints.qa import list_task_qa_runs_core
from oddish.db import TaskModel, TrialModel, TrialStatus


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    def __init__(self, task, rows):
        self.task = task
        self.rows = rows

    async def scalar(self, _statement):
        return self.task

    async def execute(self, _statement):
        return _Rows(self.rows)


def _task() -> TaskModel:
    return TaskModel(
        id="task-1",
        name="task",
        org_id="org-1",
        user="user",
        task_path="s3://tasks/task-1",
    )


def _qa_trial() -> TrialModel:
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    return TrialModel(
        id="task-1-9",
        name="task-qa-9",
        task_id="task-1",
        task_version_id="version-1",
        agent="claude-code",
        provider="fireworks",
        queue_key="fireworks/glm-5p2",
        model="fireworks/glm-5p2",
        environment="daytona",
        kind="qa",
        is_probe=False,
        status=TrialStatus.FAILED,
        attempts=1,
        max_attempts=3,
        error_message="schema violation",
        trial_s3_key="trials/task-1-9/",
        has_trajectory=True,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_qa_runs_are_compact_real_trials_with_artifact_availability():
    historical = _qa_trial()
    historical.superseded_by_trial_id = "task-1-10"
    active = _qa_trial()
    active.id = "task-1-10"
    active.name = "task-qa-10"
    active.status = TrialStatus.RUNNING
    active.trial_s3_key = None
    active.harbor_result_path = None
    rows = await list_task_qa_runs_core(
        _Session(_task(), [(active, 3), (historical, 3)]),
        task_id="task-1",
        org_id="org-1",
        version=3,
    )

    assert len(rows) == 2
    run = rows[1]
    assert run.id == "task-1-9"
    assert run.kind == "qa"
    assert run.task_version == 3
    assert run.status == TrialStatus.FAILED
    assert run.artifacts_available is True
    assert run.error_message == "schema violation"
    assert "harbor_config" not in run.model_dump()
    assert "result" not in run.model_dump()
    assert rows[0].status == TrialStatus.RUNNING
    assert rows[0].artifacts_available is False


@pytest.mark.asyncio
async def test_qa_runs_hide_missing_or_cross_org_tasks_as_not_found():
    with pytest.raises(HTTPException) as raised:
        await list_task_qa_runs_core(
            _Session(None, []),
            task_id="task-1",
            org_id="other-org",
        )

    assert raised.value.status_code == 404

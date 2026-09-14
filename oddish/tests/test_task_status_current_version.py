"""``get_task_status_core`` selects the current version's trials in SQL."""

from __future__ import annotations

import uuid

import pytest

from oddish.core.endpoints import get_task_status_core
from oddish.db.models import (
    ExperimentModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    utcnow,
)

_ORG = f"status-org-{uuid.uuid4().hex[:8]}"


def _trial(task, experiment, version_id, *, kind="agent", superseded_by=None):
    trial_id = f"tr-{uuid.uuid4().hex[:8]}"
    return TrialModel(
        id=trial_id,
        name=trial_id,
        task_id=task.id,
        experiment_id=experiment.id,
        org_id=_ORG,
        agent="claude-code",
        provider="anthropic",
        queue_key="anthropic/claude-opus-4-8",
        model="claude-opus-4-8",
        status=TrialStatus.SUCCESS,
        created_at=utcnow(),
        task_version_id=version_id,
        kind=kind,
        superseded_by_trial_id=superseded_by,
    )


@pytest.mark.asyncio
async def test_only_live_agent_trials_of_the_current_version_are_returned(session):
    task = TaskModel(
        name=f"status-task-{_ORG}", org_id=_ORG, user="tester", task_path="s3://x"
    )
    experiment = ExperimentModel(
        name=f"status-exp-{_ORG}", org_id=_ORG, last_activity_at=utcnow()
    )
    session.add_all([task, experiment])
    await session.flush()
    v1 = TaskVersionModel(
        id=f"{task.id}-v1", task_id=task.id, version=1, task_path="s3://x/v1"
    )
    v2 = TaskVersionModel(
        id=f"{task.id}-v2", task_id=task.id, version=2, task_path="s3://x/v2"
    )
    session.add_all([v1, v2])
    await session.flush()
    task.current_version_id = v2.id
    await session.flush()

    old = _trial(task, experiment, v1.id)
    current = _trial(task, experiment, v2.id)
    replaced = _trial(task, experiment, v2.id, superseded_by=current.id)
    qa = _trial(task, experiment, v2.id, kind="qa_eval")
    session.add_all([old, current, replaced, qa])
    await session.flush()

    response = await get_task_status_core(session, task_id=task.id, org_id=_ORG)

    assert [trial.id for trial in response.trials] == [current.id]

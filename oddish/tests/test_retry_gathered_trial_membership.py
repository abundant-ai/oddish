"""Regression: retrying a trial a collection gathered must not drop it.

A gathered trial keeps its home ``experiment_id``; the collection sees it only
through ``experiment_trials``. ``retry_trial_core`` copied the scalar column to
the replacement trial but not the gathered rows, then marked the old trial
superseded -- which the list queries filter out. The trial vanished from the
collection that gathered it and reappeared only in its home experiment.

``retry_trial_core`` commits, so the rows outlive the session fixture's
rollback; ``cleanup`` drops them and every name is unique per run.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from oddish.core.endpoints.collections import create_trial_collection_core
from oddish.core.endpoints.tasks_query import list_experiment_slim_tasks
from oddish.core.endpoints.trials import retry_trial_core
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    experiment_trials,
    generate_id,
)
from oddish.db.connection import get_session


@pytest_asyncio.fixture
async def cleanup():
    """Delete what the committing retry path leaves behind."""
    task_ids: list[str] = []
    experiment_ids: list[str] = []
    yield task_ids, experiment_ids
    async with get_session() as s:
        for task_id in task_ids:
            await s.execute(
                text("DELETE FROM worker_jobs WHERE subject_id LIKE :p"),
                {"p": f"{task_id}%"},
            )
            await s.execute(
                text("DELETE FROM trials WHERE task_id = :t"), {"t": task_id}
            )
            await s.execute(
                text("DELETE FROM task_versions WHERE task_id = :t"), {"t": task_id}
            )
            await s.execute(text("DELETE FROM tasks WHERE id = :t"), {"t": task_id})
        for experiment_id in experiment_ids:
            await s.execute(
                text("DELETE FROM experiments WHERE id = :e"), {"e": experiment_id}
            )
        await s.commit()


async def _gathered_trial(session, cleanup):
    """A SUCCESS trial homed in one experiment, gathered into a collection.

    Returns ``(task, trial, collection_id)``.
    """
    task_ids, experiment_ids = cleanup
    suffix = generate_id()

    task = TaskModel(
        name=f"retry-gather-task-{suffix}",
        org_id="org1",
        user="tester",
        task_path="s3://tasks/retry-gather-task",
    )
    session.add(task)
    await session.flush()
    task_ids.append(task.id)

    version = TaskVersionModel(
        id=f"{task.id}-v1", task_id=task.id, version=1, task_path=task.task_path
    )
    session.add(version)
    await session.flush()
    task.current_version_id = version.id

    home = ExperimentModel(name=f"retry-gather-home-{suffix}", org_id="org1")
    session.add(home)
    await session.flush()
    experiment_ids.append(home.id)

    trial_id = generate_id()
    trial = TrialModel(
        id=trial_id,
        name=trial_id,
        task_id=task.id,
        task_version_id=version.id,
        experiment_id=home.id,
        org_id="org1",
        agent="codex",
        provider="openai",
        queue_key="openai/gpt-5.5",
        model="gpt-5.5",
        status=TrialStatus.SUCCESS,
        reward=1,
    )
    session.add(trial)
    await session.flush()

    collection = await create_trial_collection_core(
        session,
        name=f"retry-gather-collection-{suffix}",
        trial_ids=[trial.id],
        org_id="org1",
    )
    await session.flush()
    experiment_ids.append(collection.id)
    return task, trial, collection.id


@pytest.mark.asyncio
async def test_retry_keeps_replacement_in_the_gathering_collection(session, cleanup):
    task, trial, collection_id = await _gathered_trial(session, cleanup)

    result = await retry_trial_core(
        session, trial_id=trial.id, org_id="org1", gate_baselines=False
    )
    new_trial_id = result["trial_id"]

    gathered = (
        await session.scalars(
            select(experiment_trials.c.trial_id).where(
                experiment_trials.c.experiment_id == collection_id,
                experiment_trials.c.deleted_at.is_(None),
            )
        )
    ).all()
    assert new_trial_id in gathered, "replacement trial was not gathered"

    responses = await list_experiment_slim_tasks(
        session, experiment_id=collection_id, org_id="org1"
    )
    by_task = {r.id: r for r in responses}
    assert task.id in by_task, "task disappeared from the collection grid"
    trial_ids = [t.id for t in (by_task[task.id].trials or [])]
    assert new_trial_id in trial_ids, "retried trial disappeared from the collection"
    assert trial.id not in trial_ids, "superseded trial should be replaced, not kept"


@pytest.mark.asyncio
async def test_retry_leaves_home_experiment_unchanged(session, cleanup):
    """The replacement stays homed where the old trial was: gathering is
    additive, so a retry must not re-home the trial onto the collection."""
    _, trial, _ = await _gathered_trial(session, cleanup)
    home_id = trial.experiment_id

    result = await retry_trial_core(
        session, trial_id=trial.id, org_id="org1", gate_baselines=False
    )

    new_trial = await session.get(TrialModel, result["trial_id"])
    assert new_trial.experiment_id == home_id

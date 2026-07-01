from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.endpoints.collections import create_trial_collection_core
from oddish.core.endpoints.tasks_query import list_experiment_slim_tasks
from oddish.db.models import ExperimentModel, TaskModel, TrialModel, generate_id


def _task(name: str, *, org_id: str = "org1") -> TaskModel:
    return TaskModel(
        name=name,
        org_id=org_id,
        user="tester",
        task_path=f"s3://tasks/{name}",
    )


def _experiment(name: str, *, org_id: str = "org1") -> ExperimentModel:
    return ExperimentModel(name=name, org_id=org_id)


def _trial(
    task: TaskModel,
    home_experiment: ExperimentModel,
    *,
    org_id: str = "org1",
    is_probe: bool = False,
) -> TrialModel:
    trial_id = generate_id()
    return TrialModel(
        id=trial_id,
        name=trial_id,
        task_id=task.id,
        experiment_id=home_experiment.id,
        org_id=org_id,
        agent="codex",
        provider="openai",
        queue_key="openai/gpt-5.5",
        model="gpt-5.5",
        is_probe=is_probe,
    )


@pytest.mark.asyncio
async def test_slim_tasks_returns_gathered_trials(session):
    task = _task("detail-collection-task-1")
    session.add(task)
    await session.flush()

    home = _experiment("home-detail-1")
    session.add(home)
    await session.flush()

    t1 = _trial(task, home, org_id="org1")
    session.add(t1)
    await session.flush()

    coll = await create_trial_collection_core(
        session, name="c", trial_ids=[t1.id], org_id="org1"
    )
    await session.flush()

    tasks = await list_experiment_slim_tasks(
        session, experiment_id=coll.id, org_id="org1"
    )
    all_trial_ids = {tr.id for task in tasks for tr in task.trials}
    assert t1.id in all_trial_ids


@pytest.mark.asyncio
async def test_slim_tasks_excludes_probe_gathered_trials(session):
    task = _task("detail-collection-task-2")
    session.add(task)
    await session.flush()

    home = _experiment("home-detail-2")
    session.add(home)
    await session.flush()

    probe = _trial(task, home, org_id="org1", is_probe=True)
    session.add(probe)
    await session.flush()

    coll = await create_trial_collection_core(
        session, name="c", trial_ids=[probe.id], org_id="org1"
    )
    await session.flush()

    tasks = await list_experiment_slim_tasks(
        session, experiment_id=coll.id, org_id="org1"
    )
    all_trial_ids = {tr.id for task in tasks for tr in task.trials}
    assert probe.id not in all_trial_ids

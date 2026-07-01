from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.endpoints.collections import create_trial_collection_core
from oddish.db import experiment_trials, task_experiments
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


def _trial(task: TaskModel, home_experiment: ExperimentModel, *, org_id: str = "org1") -> TrialModel:
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
    )


@pytest.mark.asyncio
async def test_creates_collection_links_trials_and_tasks(session):
    task = _task("collection-task-1")
    session.add(task)
    await session.flush()

    home = _experiment("home-1")
    session.add(home)
    await session.flush()

    t1 = _trial(task, home, org_id="org1")
    t2 = _trial(task, home, org_id="org1")
    session.add_all([t1, t2])
    await session.flush()

    resp = await create_trial_collection_core(
        session, name="my collection", trial_ids=[t1.id, t2.id], org_id="org1"
    )
    await session.flush()

    exp = (
        await session.execute(
            select(ExperimentModel).where(ExperimentModel.id == resp.id)
        )
    ).scalar_one()
    assert exp.is_collection is True
    assert exp.org_id == "org1"

    linked = (
        await session.execute(
            select(experiment_trials.c.trial_id).where(
                experiment_trials.c.experiment_id == resp.id
            )
        )
    ).scalars().all()
    assert set(linked) == {t1.id, t2.id}

    tasks = (
        await session.execute(
            select(task_experiments.c.task_id).where(
                task_experiments.c.experiment_id == resp.id
            )
        )
    ).scalars().all()
    assert set(tasks) == {task.id}
    assert resp.trials_linked == 2
    assert resp.tasks_linked == 1


@pytest.mark.asyncio
async def test_rejects_unknown_trial_id(session):
    task = _task("collection-task-2")
    session.add(task)
    await session.flush()

    home = _experiment("home-2")
    session.add(home)
    await session.flush()

    t1 = _trial(task, home, org_id="org1")
    session.add(t1)
    await session.flush()

    with pytest.raises(HTTPException) as exc:
        await create_trial_collection_core(
            session, name="c", trial_ids=[t1.id, "nope"], org_id="org1"
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_rejects_cross_org_trial(session):
    task = _task("collection-task-3", org_id="org2")
    session.add(task)
    await session.flush()

    home = _experiment("home-3", org_id="org2")
    session.add(home)
    await session.flush()

    other = _trial(task, home, org_id="org2")
    session.add(other)
    await session.flush()

    with pytest.raises(HTTPException) as exc:
        await create_trial_collection_core(
            session, name="c", trial_ids=[other.id], org_id="org1"
        )
    assert exc.value.status_code == 404

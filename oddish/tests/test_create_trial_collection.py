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


from datetime import datetime, timezone

from oddish.db.models import TaskVersionModel, TrialStatus


def _version(task, n: int) -> TaskVersionModel:
    return TaskVersionModel(
        id=f"{task.id}-v{n}", task_id=task.id, version=n, task_path=f"s3://t/{task.id}/v{n}"
    )


def _ver_trial(task, home, version_id, *, status=TrialStatus.SUCCESS,
               is_probe=False, superseded=None, org_id="org1") -> TrialModel:
    t = _trial(task, home, org_id=org_id)
    t.task_version_id = version_id
    t.status = status
    t.is_probe = is_probe
    t.superseded_by_trial_id = superseded
    return t


@pytest.mark.asyncio
async def test_task_mode_links_only_current_version_terminal_trials(session):
    task = _task("cbt-task")
    session.add(task)
    await session.flush()
    v1, v2 = _version(task, 1), _version(task, 2)
    session.add_all([v1, v2])
    await session.flush()
    task.current_version_id = v2.id
    await session.flush()

    home = _experiment("home")
    session.add(home)
    await session.flush()

    keep_a = _ver_trial(task, home, v2.id, status=TrialStatus.SUCCESS)
    keep_b = _ver_trial(task, home, v2.id, status=TrialStatus.FAILED)
    old = _ver_trial(task, home, v1.id, status=TrialStatus.SUCCESS)       # old version
    pending = _ver_trial(task, home, v2.id, status=TrialStatus.PENDING)   # not terminal
    probe = _ver_trial(task, home, v2.id, is_probe=True)                  # probe
    sup = _ver_trial(task, home, v2.id, superseded=keep_a.id)             # superseded
    session.add_all([keep_a, keep_b, old, pending, probe, sup])
    await session.flush()

    resp = await create_trial_collection_core(
        session, name="c", task_ids=[task.name], org_id="org1"
    )
    await session.flush()

    linked = set((await session.execute(
        select(experiment_trials.c.trial_id).where(
            experiment_trials.c.experiment_id == resp.id
        )
    )).scalars().all())
    assert linked == {keep_a.id, keep_b.id}
    assert resp.trials_linked == 2
    assert resp.trials_from_tasks == 2


@pytest.mark.asyncio
async def test_task_not_found_raises_404(session):
    with pytest.raises(HTTPException) as ei:
        await create_trial_collection_core(
            session, name="c", task_ids=["nope"], org_id="org1"
        )
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_empty_task_is_skipped_and_counted(session):
    task = _task("empty-task")
    session.add(task)
    await session.flush()
    v1 = _version(task, 1)
    session.add(v1)
    await session.flush()
    task.current_version_id = v1.id
    await session.flush()
    # no trials for v1 -> whole set empty -> 400
    with pytest.raises(HTTPException) as ei:
        await create_trial_collection_core(
            session, name="c", task_ids=[task.name], org_id="org1"
        )
    assert ei.value.status_code == 400

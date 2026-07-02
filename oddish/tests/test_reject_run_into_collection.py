"""create_task must reject runs targeting a collection experiment.

Collections are read-only view objects (a fixed set of trials/tasks gathered
via ``create_trial_collection_core``); homing a real trial on one would
corrupt collection semantics and the cost-rollup invariant.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.endpoints import create_task_sweep_core
from oddish.core.endpoints.collections import create_trial_collection_core
from oddish.db.models import ExperimentModel, TaskModel, TrialModel, generate_id
from oddish.queue import create_task
from oddish.schemas import (
    AgentModelPair,
    TaskSubmission,
    TaskSweepSubmission,
    TrialSpec,
)


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
    task: TaskModel, home_experiment: ExperimentModel, *, org_id: str = "org1"
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
    )


def _submission(name: str, *, experiment_id: str | None) -> TaskSubmission:
    return TaskSubmission(
        name=name,
        task_path="s3://test-bucket/reject-run-into-collection-fake-task",
        user="test",
        experiment_id=experiment_id,
        trials=[TrialSpec(agent="nop", model=None)],
    )


@pytest.mark.asyncio
async def test_run_into_collection_rejected_by_id(session):
    task = _task("reject-collection-task-1")
    session.add(task)
    await session.flush()

    home = _experiment("reject-collection-home-1")
    session.add(home)
    await session.flush()

    t1 = _trial(task, home, org_id="org1")
    session.add(t1)
    await session.flush()

    coll = await create_trial_collection_core(
        session, name="reject-collection-1", trial_ids=[t1.id], org_id="org1"
    )
    await session.flush()

    with pytest.raises(ValueError) as exc_info:
        await create_task(
            session,
            _submission("run-into-collection-1", experiment_id=coll.id),
            org_id="org1",
        )

    assert "collection" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_run_into_collection_rejected_by_name(session):
    task = _task("reject-collection-task-2")
    session.add(task)
    await session.flush()

    home = _experiment("reject-collection-home-2")
    session.add(home)
    await session.flush()

    t1 = _trial(task, home, org_id="org1")
    session.add(t1)
    await session.flush()

    coll = await create_trial_collection_core(
        session, name="reject-collection-2", trial_ids=[t1.id], org_id="org1"
    )
    await session.flush()

    with pytest.raises(ValueError) as exc_info:
        await create_task(
            session,
            _submission("run-into-collection-2", experiment_id=coll.name),
            org_id="org1",
        )

    assert "collection" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_run_into_normal_experiment_still_succeeds(session):
    """Control: a non-collection experiment target is unaffected by the guard."""
    home = _experiment("reject-collection-control-home")
    session.add(home)
    await session.flush()

    task = await create_task(
        session,
        _submission("run-into-normal-experiment", experiment_id=home.id),
        org_id="org1",
    )
    await session.flush()

    assert task.id is not None


def _sweep_append(task_id: str, *, experiment_id: str) -> TaskSweepSubmission:
    return TaskSweepSubmission(
        task_id=task_id,
        append_to_task=True,
        experiment_id=experiment_id,
        configs=[AgentModelPair(agent="nop", model=None, n_trials=1)],
        user="test",
    )


@pytest.mark.asyncio
async def test_sweep_append_into_collection_rejected(session):
    """Regression for Fix 1: ``oddish run <task> --experiment <collection_id>``
    goes through ``/tasks/sweep`` (append branch), not ``create_task`` directly.
    Gathering into a collection leaves the trial's parent task intact, so the
    append branch's auto-detect fires and must be guarded too."""
    task = _task("reject-collection-sweep-task-1")
    session.add(task)
    await session.flush()

    home = _experiment("reject-collection-sweep-home-1")
    session.add(home)
    await session.flush()

    t1 = _trial(task, home, org_id="org1")
    session.add(t1)
    await session.flush()

    coll = await create_trial_collection_core(
        session, name="reject-collection-sweep-1", trial_ids=[t1.id], org_id="org1"
    )
    await session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await create_task_sweep_core(
            session,
            submission=_sweep_append(task.id, experiment_id=coll.id),
            org_id="org1",
        )

    assert exc_info.value.status_code == 404
    assert "collection" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_sweep_append_into_normal_experiment_still_succeeds(session):
    """Control: a normal (non-collection) sweep-append still works."""
    task = _task("reject-collection-sweep-task-2")
    session.add(task)
    await session.flush()

    home = _experiment("reject-collection-sweep-home-2")
    session.add(home)
    await session.flush()

    t1 = _trial(task, home, org_id="org1")
    session.add(t1)
    await session.flush()

    target = _experiment("reject-collection-sweep-target-2")
    session.add(target)
    await session.flush()

    _, new_trials, is_append, experiment = await create_task_sweep_core(
        session,
        submission=_sweep_append(task.id, experiment_id=target.id),
        org_id="org1",
    )

    assert is_append is True
    assert len(new_trials) == 1
    assert experiment is not None and experiment.id == target.id

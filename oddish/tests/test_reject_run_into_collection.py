"""create_task must reject runs targeting a collection experiment.

Collections are read-only view objects (a fixed set of trials/tasks gathered
via ``create_trial_collection_core``); homing a real trial on one would
corrupt collection semantics and the cost-rollup invariant.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.endpoints.collections import create_trial_collection_core
from oddish.db.models import ExperimentModel, TaskModel, TrialModel, generate_id
from oddish.queue import create_task
from oddish.schemas import TaskSubmission, TrialSpec


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

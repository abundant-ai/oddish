from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.endpoints.collections import create_trial_collection_core
from oddish.core.endpoints.deletion import _combine_idempotency_key
from oddish.core.endpoints.experiment_page import get_experiment_trial_page_core
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
@pytest.mark.parametrize("source_id", ["source-short", "source-" + "s" * 80])
async def test_trial_page_collapses_gathered_combine_copies(session, source_id):
    task = _task("detail-collection-task-1")
    session.add(task)
    await session.flush()

    source_home = _experiment("source-detail-1")
    copy_home = _experiment("copy-detail-1")
    session.add_all([source_home, copy_home])
    await session.flush()

    original = _trial(task, source_home)
    original.id = original.name = source_id
    copy = _trial(task, copy_home)
    copy.idempotency_key = _combine_idempotency_key(copy_home.id, original.id)
    session.add_all([original, copy])
    await session.flush()

    coll = await create_trial_collection_core(
        session, name="c", trial_ids=[original.id, copy.id], org_id="org1"
    )
    lone_copy = await create_trial_collection_core(
        session, name="lone copy", trial_ids=[copy.id], org_id="org1"
    )

    page = await get_experiment_trial_page_core(
        session, experiment_id=coll.id, org_id="org1"
    )
    assert {trial.id for trial in page.trials} == {original.id}

    session.expire_all()
    page = await get_experiment_trial_page_core(
        session, experiment_id=lone_copy.id, org_id="org1"
    )
    assert {trial.id for trial in page.trials} == {copy.id}


@pytest.mark.asyncio
async def test_trial_page_excludes_probe_gathered_trials(session):
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

    page = await get_experiment_trial_page_core(
        session, experiment_id=coll.id, org_id="org1"
    )
    assert probe.id not in {trial.id for trial in page.trials}

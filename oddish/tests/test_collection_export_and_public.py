"""Task 7: union gathered trials into the export path and the public/share
path for collection experiments, while keeping probes out of public views.

Site 1 (export): ``list_experiment_trials_for_org`` must include trials
gathered into a collection via ``experiment_trials``, not just trials whose
home ``experiment_id`` matches.

Site 2 (public/share): ``list_public_experiment_tasks`` must also admit
gathered trials, but ``is_probe`` trials must still never appear -- gathered
or not.
"""

from __future__ import annotations

import uuid

import pytest

from oddish.core.endpoints.collections import create_trial_collection_core
from oddish.core.sharing.helpers import (
    ensure_experiment_public,
    list_experiment_trials_for_org,
)
from oddish.core.sharing.public import list_public_experiment_tasks
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TrialModel,
    experiment_trials,
    generate_id,
    get_session,
    task_experiments,
)
from sqlalchemy import select


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


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


async def _cleanup(
    *,
    task_ids: list[str] | None = None,
    experiment_ids: list[str] | None = None,
) -> None:
    async with get_session() as session:
        if experiment_ids:
            await session.execute(
                experiment_trials.delete().where(
                    experiment_trials.c.experiment_id.in_(experiment_ids)
                )
            )
        if task_ids:
            await session.execute(
                task_experiments.delete().where(
                    task_experiments.c.task_id.in_(task_ids)
                )
            )
            await session.execute(
                TaskModel.__table__.delete().where(TaskModel.id.in_(task_ids))
            )
        if experiment_ids:
            await session.execute(
                ExperimentModel.__table__.delete().where(
                    ExperimentModel.id.in_(experiment_ids)
                )
            )


@pytest.mark.asyncio
async def test_export_lists_gathered_trials(session):
    """Site 1: export lists trials gathered into a collection experiment,
    not just trials whose home experiment_id matches."""
    task = _task(_unique("export-task"))
    session.add(task)
    await session.flush()

    home = _experiment(_unique("export-home"))
    session.add(home)
    await session.flush()

    t1 = _trial(task, home, org_id="org1")
    session.add(t1)
    await session.flush()

    coll = await create_trial_collection_core(
        session, name="c", trial_ids=[t1.id], org_id="org1"
    )
    await session.flush()

    trials = await list_experiment_trials_for_org(session, coll.id, org_id="org1")
    assert t1.id in {t.id for t in trials}


@pytest.mark.asyncio
async def test_public_view_strips_gathered_probe():
    """Site 2: a gathered probe trial must never appear in the public view,
    even though its task is admitted via the gathered-trial union."""
    task_ids: list[str] = []
    experiment_ids: list[str] = []
    try:
        async with get_session() as setup:
            task = _task(_unique("public-probe-task"))
            setup.add(task)
            await setup.flush()

            home = _experiment(_unique("public-probe-home"))
            setup.add(home)
            await setup.flush()

            probe = _trial(task, home, org_id="org1", is_probe=True)
            setup.add(probe)
            await setup.flush()

            coll = await create_trial_collection_core(
                setup, name=_unique("coll"), trial_ids=[probe.id], org_id="org1"
            )
            await setup.flush()

            task_ids = [task.id]
            experiment_ids = [home.id, coll.id]

            coll_exp = (
                await setup.execute(
                    select(ExperimentModel).where(ExperimentModel.id == coll.id)
                )
            ).scalar_one()
            await ensure_experiment_public(setup, coll_exp)
            public_token = coll_exp.public_token
            # get_session() commits on clean exit.

        tasks = await list_public_experiment_tasks(public_token)
        ids = {tr.id for t in tasks for tr in t.trials}
        assert probe.id not in ids
    finally:
        await _cleanup(task_ids=task_ids, experiment_ids=experiment_ids)


@pytest.mark.asyncio
async def test_public_view_includes_gathered_non_probe():
    """Site 2: a gathered (non-probe) trial DOES appear in the public view,
    even though its home experiment differs from the collection."""
    task_ids: list[str] = []
    experiment_ids: list[str] = []
    try:
        async with get_session() as setup:
            task = _task(_unique("public-gathered-task"))
            setup.add(task)
            await setup.flush()

            home = _experiment(_unique("public-gathered-home"))
            setup.add(home)
            await setup.flush()

            t1 = _trial(task, home, org_id="org1", is_probe=False)
            setup.add(t1)
            await setup.flush()

            coll = await create_trial_collection_core(
                setup, name=_unique("coll"), trial_ids=[t1.id], org_id="org1"
            )
            await setup.flush()

            task_ids = [task.id]
            experiment_ids = [home.id, coll.id]

            coll_exp = (
                await setup.execute(
                    select(ExperimentModel).where(ExperimentModel.id == coll.id)
                )
            ).scalar_one()
            await ensure_experiment_public(setup, coll_exp)
            public_token = coll_exp.public_token

        tasks = await list_public_experiment_tasks(public_token)
        ids = {tr.id for t in tasks for tr in t.trials}
        assert t1.id in ids
    finally:
        await _cleanup(task_ids=task_ids, experiment_ids=experiment_ids)

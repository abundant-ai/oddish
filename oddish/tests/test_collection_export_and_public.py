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
    generate_public_token,
    get_public_experiment,
    get_public_task_for_experiment,
    get_public_trial,
    get_public_trial_for_experiment,
    list_task_trials_for_public_experiment,
    list_experiment_trials_for_org,
)
from oddish.core.sharing.public import (
    list_public_experiment_tasks,
    list_public_experiments,
)
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    experiment_trials,
    generate_id,
    get_session,
    task_experiments,
)
from sqlalchemy import select


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def test_generate_public_token_uses_256_bit_urlsafe_secret():
    token = generate_public_token()

    assert len(token) == 43
    assert token.replace("-", "").replace("_", "").isalnum()


def _task(name: str, *, org_id: str = "org1") -> TaskModel:
    return TaskModel(
        name=name,
        org_id=org_id,
        user="tester",
        task_path=f"s3://tasks/{name}",
    )


def _experiment(name: str, *, org_id: str = "org1") -> ExperimentModel:
    return ExperimentModel(name=name, org_id=org_id)


def _version(task: TaskModel, n: int) -> TaskVersionModel:
    return TaskVersionModel(
        id=f"{task.id}-v{n}",
        task_id=task.id,
        version=n,
        task_path=task.task_path,
    )


def _trial(
    task: TaskModel,
    home_experiment: ExperimentModel,
    *,
    org_id: str = "org1",
    is_probe: bool = False,
    task_version_id: str | None = None,
) -> TrialModel:
    trial_id = generate_id()
    return TrialModel(
        id=trial_id,
        name=trial_id,
        task_id=task.id,
        task_version_id=task_version_id,
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
async def test_public_experiments_list_does_not_enumerate_share_tokens():
    exp_id: str | None = None
    exp_name: str | None = None
    token: str | None = None
    try:
        async with get_session() as setup:
            exp = _experiment(_unique("private-link"), org_id=None)
            setup.add(exp)
            await setup.flush()

            await ensure_experiment_public(setup, exp)
            await setup.flush()
            exp_id = exp.id
            exp_name = exp.name
            token = exp.public_token
            assert token

        listed = await list_public_experiments()
        assert listed == []

        async with get_session() as verify:
            resolved = await get_public_experiment(verify, token)
            assert resolved is not None
            assert resolved.public_token == token
            assert resolved.name == exp_name
    finally:
        if exp_id:
            await _cleanup(experiment_ids=[exp_id])


@pytest.mark.asyncio
async def test_republish_after_unpublish_generates_fresh_public_token(session):
    exp = _experiment(_unique("rotating-link"), org_id=None)
    session.add(exp)
    await session.flush()

    await ensure_experiment_public(session, exp)
    old_token = exp.public_token
    assert old_token

    exp.is_public = False
    exp.public_token = None
    await session.flush()
    assert await get_public_experiment(session, old_token) is None

    await ensure_experiment_public(session, exp)
    new_token = exp.public_token

    assert new_token
    assert new_token != old_token
    assert await get_public_experiment(session, old_token) is None
    assert await get_public_experiment(session, new_token) is not None


@pytest.mark.asyncio
async def test_public_trial_access_is_scoped_to_share_token(session):
    task = _task(_unique("scoped-public-task"), org_id=None)
    session.add(task)
    await session.flush()

    exp_a = _experiment(_unique("scoped-public-a"), org_id=None)
    exp_b = _experiment(_unique("scoped-public-b"), org_id=None)
    session.add_all([exp_a, exp_b])
    await session.flush()
    await session.execute(
        task_experiments.insert(),
        [
            {"task_id": task.id, "experiment_id": exp_a.id},
            {"task_id": task.id, "experiment_id": exp_b.id},
        ],
    )

    trial = _trial(task, exp_a, org_id=None, is_probe=False)
    session.add(trial)
    await session.flush()

    await ensure_experiment_public(session, exp_a)
    await ensure_experiment_public(session, exp_b)
    await session.flush()
    token_a = exp_a.public_token
    token_b = exp_b.public_token
    assert token_a and token_b

    assert await get_public_task_for_experiment(session, token_a, task.id) is not None
    assert await get_public_task_for_experiment(session, token_b, task.id) is not None

    visible = await get_public_trial_for_experiment(session, token_a, trial.id)
    assert visible is not None
    assert visible.id == trial.id
    assert await get_public_trial_for_experiment(session, token_b, trial.id) is None

    trials_a = await list_task_trials_for_public_experiment(session, token_a, task.id)
    trials_b = await list_task_trials_for_public_experiment(session, token_b, task.id)
    assert trials_a is not None and [row.id for row in trials_a] == [trial.id]
    assert trials_b == []


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


@pytest.mark.asyncio
async def test_public_view_surfaces_gathered_old_version_trial():
    """Site 2 REGRESSION: a trial gathered from an OLDER task version must
    still appear in the public collection view, even after the task's
    current_version_id has moved on to a newer version.

    ``list_public_experiment_tasks`` already unions gathered trials into
    ``task.trials`` (Task 7), but it re-derived the "effective version" via
    ``build_task_status_response`` without telling it which trials were
    gathered -- so the resolver fell back to the task's current version and
    filtered the gathered v1 trial right back out (the same double-filter,
    now on the public surface)."""
    task_ids: list[str] = []
    experiment_ids: list[str] = []
    try:
        async with get_session() as setup:
            task = _task(_unique("public-oldver-task"))
            setup.add(task)
            await setup.flush()

            v1 = _version(task, 1)
            v2 = _version(task, 2)
            setup.add_all([v1, v2])
            await setup.flush()

            # Task re-uploaded/bumped -- current version is now the NEWER v2.
            task.current_version_id = v2.id
            await setup.flush()

            home = _experiment(_unique("public-oldver-home"))
            setup.add(home)
            await setup.flush()

            # Trial ran on the OLDER v1, homed privately.
            t1 = _trial(task, home, org_id="org1", task_version_id=v1.id)
            probe = _trial(
                task, home, org_id="org1", is_probe=True, task_version_id=v1.id
            )
            setup.add_all([t1, probe])
            await setup.flush()

            coll = await create_trial_collection_core(
                setup,
                name=_unique("coll"),
                trial_ids=[t1.id, probe.id],
                org_id="org1",
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
        by_task = {t.id: t for t in tasks}
        assert task.id in by_task
        trial_ids = {tr.id for tr in by_task[task.id].trials}
        assert t1.id in trial_ids, (
            "gathered v1 trial was filtered out of the public view "
            "(gathered-unaware effective-version double-filter)"
        )
        # Probe strip is independent of the version fix -- reconfirm it holds.
        assert probe.id not in trial_ids
    finally:
        await _cleanup(task_ids=task_ids, experiment_ids=experiment_ids)


@pytest.mark.asyncio
async def test_get_public_trial_resolves_gathered_non_probe(session):
    """Fix 2: ``get_public_trial`` must admit a trial whose HOME experiment is
    private but whose GATHERED (collection) experiment is public -- otherwise
    the per-trial detail/logs/files/result endpoints 404 for trials that
    render fine in the collection's public grid."""
    task = _task(_unique("public-trial-task"))
    session.add(task)
    await session.flush()

    home = _experiment(_unique("public-trial-home"))  # stays private
    session.add(home)
    await session.flush()

    t1 = _trial(task, home, org_id="org1", is_probe=False)
    session.add(t1)
    await session.flush()

    coll = await create_trial_collection_core(
        session, name=_unique("coll"), trial_ids=[t1.id], org_id="org1"
    )
    await session.flush()

    coll_exp = (
        await session.execute(
            select(ExperimentModel).where(ExperimentModel.id == coll.id)
        )
    ).scalar_one()
    await ensure_experiment_public(session, coll_exp)
    await session.flush()

    assert home.is_public is False  # home experiment never published

    resolved = await get_public_trial(session, t1.id)
    assert resolved is not None
    assert resolved.id == t1.id


@pytest.mark.asyncio
async def test_get_public_trial_resolves_home_public(session):
    """Fix B: a trial homed directly in a public experiment (never gathered
    into any collection) must still resolve via ``get_public_trial``."""
    task = _task(_unique("public-trial-home-task"))
    session.add(task)
    await session.flush()

    home = _experiment(_unique("public-trial-home-only"))
    session.add(home)
    await session.flush()

    t1 = _trial(task, home, org_id="org1", is_probe=False)
    session.add(t1)
    await session.flush()

    await ensure_experiment_public(session, home)
    await session.flush()

    assert home.is_public is True

    resolved = await get_public_trial(session, t1.id)
    assert resolved is not None
    assert resolved.id == t1.id


@pytest.mark.asyncio
async def test_get_public_trial_excludes_gathered_probe(session):
    """Anti-leak: even once gathered into a PUBLIC collection, a probe trial
    must never resolve via ``get_public_trial``. Probes are internal-only and
    must never appear on any public surface."""
    task = _task(_unique("public-probe-trial-task"))
    session.add(task)
    await session.flush()

    home = _experiment(_unique("public-probe-trial-home"))
    session.add(home)
    await session.flush()

    probe = _trial(task, home, org_id="org1", is_probe=True)
    session.add(probe)
    await session.flush()

    coll = await create_trial_collection_core(
        session, name=_unique("coll"), trial_ids=[probe.id], org_id="org1"
    )
    await session.flush()

    coll_exp = (
        await session.execute(
            select(ExperimentModel).where(ExperimentModel.id == coll.id)
        )
    ).scalar_one()
    await ensure_experiment_public(session, coll_exp)
    await session.flush()

    resolved = await get_public_trial(session, probe.id)
    assert resolved is None

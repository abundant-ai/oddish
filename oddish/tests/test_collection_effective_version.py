"""End-to-end regression: a collection experiment must surface trials it
gathered from an OLDER task version, even after the underlying task has been
re-uploaded and bumped to a newer ``current_version_id``.

Trials are gathered additively via the ``experiment_trials`` join table without
rewriting each trial's scalar ``experiment_id``, so the effective-version
resolvers (which historically keyed off that scalar column) didn't recognize
gathered trials -- the experiment page resolved the task's *current* version and
then filtered the older gathered trial right back out. These tests exercise the
public core functions (``list_experiment_slim_tasks`` and the compact
``list_tasks_core`` experiment path), not just the resolver, so they catch that
double-filter.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core import helpers
from oddish.core.endpoints.collections import create_trial_collection_core
from oddish.core.endpoints.tasks_query import (
    list_experiment_slim_tasks,
    list_tasks_core,
)
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    generate_id,
    task_experiments,
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
    task_version_id: str,
    org_id: str = "org1",
    reward: float | None = 1,
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
        status=TrialStatus.SUCCESS,
        reward=reward,
    )


async def _build_old_version_collection(session):
    """Task bumped to v2; a trial ran on v1; gather that trial into a collection.

    Returns ``(task, v1_id, v2_id, trial, collection)``.
    """
    task = _task("coll-oldver-task")
    session.add(task)
    await session.flush()

    v1 = _version(task, 1)
    v2 = _version(task, 2)
    session.add_all([v1, v2])
    await session.flush()

    # Task's global current version is the NEWER v2.
    task.current_version_id = v2.id
    await session.flush()

    home = _experiment("coll-oldver-home")
    session.add(home)
    await session.flush()

    # The trial ran on the OLDER v1 under its home experiment.
    trial = _trial(task, home, task_version_id=v1.id)
    session.add(trial)
    await session.flush()

    resp = await create_trial_collection_core(
        session, name="oldver collection", trial_ids=[trial.id], org_id="org1"
    )
    await session.flush()

    collection = await session.get(ExperimentModel, resp.id)
    return task, v1.id, v2.id, trial, collection


@pytest.mark.asyncio
async def test_slim_path_surfaces_gathered_old_version_trial(session):
    """REGRESSION: the gathered v1 trial must appear on the collection's slim
    experiment grid, even though the task's current version is v2."""
    task, v1_id, v2_id, trial, collection = await _build_old_version_collection(session)

    responses = await list_experiment_slim_tasks(
        session, experiment_id=collection.id, org_id="org1"
    )

    by_task = {r.id: r for r in responses}
    assert task.id in by_task, "gathered task missing from collection grid"
    task_resp = by_task[task.id]
    trial_ids = [t.id for t in (task_resp.trials or [])]
    assert trial.id in trial_ids, "gathered v1 trial was filtered out (double-filter)"
    # Effective version reported is the gathered trial's own version (v1), not v2.
    assert task_resp.current_version_id == v1_id


@pytest.mark.asyncio
async def test_compact_path_surfaces_gathered_old_version_trial(session):
    """REGRESSION: same via the compact ``list_tasks_core`` experiment path."""
    task, v1_id, v2_id, trial, collection = await _build_old_version_collection(session)

    responses = await list_tasks_core(
        session,
        experiment_id=collection.id,
        compact_trials=True,
        include_queue_info=False,
        include_worker_jobs=False,
        org_id="org1",
    )

    by_task = {r.id: r for r in responses}
    assert task.id in by_task
    task_resp = by_task[task.id]
    trial_ids = [t.id for t in (task_resp.trials or [])]
    assert trial.id in trial_ids, "gathered v1 trial hidden on compact experiment path"
    assert task_resp.current_version_id == v1_id


@pytest.mark.asyncio
async def test_resolve_effective_version_uses_gathered_trial_version(session):
    """The effective version derives from the gathered trial's version (v1)."""
    task, v1_id, v2_id, trial, collection = await _build_old_version_collection(session)

    # Simulate the loaded, experiment-scoped ``task.trials`` the resolver reads:
    # the gathered trial carries its HOME experiment id, not the collection's.
    from types import SimpleNamespace

    scoped_task = SimpleNamespace(
        current_version_id=v2_id,
        trials=[
            SimpleNamespace(
                id=trial.id,
                task_version_id=v1_id,
                experiment_id=trial.experiment_id,
            )
        ],
    )

    # Gathered-unaware -> falls back to current v2 (the old blind spot).
    assert (
        helpers.resolve_effective_version_id(
            scoped_task, experiment_context_id=collection.id
        )
        == v2_id
    )
    # Gathered-aware -> the trial's own v1.
    assert (
        helpers.resolve_effective_version_id(
            scoped_task,
            experiment_context_id=collection.id,
            gathered_trial_ids={trial.id},
        )
        == v1_id
    )


@pytest.mark.asyncio
async def test_normal_experiment_unchanged_control(session):
    """No-regression control: a normal (non-collection) experiment whose trial
    ran on the task's current version resolves and surfaces exactly as before
    (the gathered set is empty)."""
    task = _task("normal-exp-task")
    session.add(task)
    await session.flush()

    v1 = _version(task, 1)
    session.add(v1)
    await session.flush()
    task.current_version_id = v1.id
    await session.flush()

    exp = _experiment("normal-exp")
    session.add(exp)
    await session.flush()

    trial = _trial(task, exp, task_version_id=v1.id)
    session.add(trial)
    # A normal experiment owns its task via the association table directly.
    await session.execute(
        task_experiments.insert().values(task_id=task.id, experiment_id=exp.id)
    )
    await session.flush()

    slim = await list_experiment_slim_tasks(
        session, experiment_id=exp.id, org_id="org1"
    )
    by_task = {r.id: r for r in slim}
    assert task.id in by_task
    task_resp = by_task[task.id]
    assert [t.id for t in (task_resp.trials or [])] == [trial.id]
    assert task_resp.current_version_id == v1.id


async def _build_multi_version_collection(session):
    """Trials ran on BOTH v1 and v2; gather all of them into one collection.

    Returns ``(task, v1_id, v2_id, v1_trial, v2_trial, collection)``.
    """
    task = _task("coll-multiver-task")
    session.add(task)
    await session.flush()

    v1 = _version(task, 1)
    v2 = _version(task, 2)
    session.add_all([v1, v2])
    await session.flush()
    task.current_version_id = v2.id
    await session.flush()

    home = _experiment("coll-multiver-home")
    session.add(home)
    await session.flush()

    v1_trial = _trial(task, home, task_version_id=v1.id)
    v2_trial = _trial(task, home, task_version_id=v2.id)
    session.add_all([v1_trial, v2_trial])
    await session.flush()

    resp = await create_trial_collection_core(
        session,
        name="multiver collection",
        trial_ids=[v1_trial.id, v2_trial.id],
        org_id="org1",
    )
    await session.flush()

    collection = await session.get(ExperimentModel, resp.id)
    return task, v1.id, v2.id, v1_trial, v2_trial, collection


@pytest.mark.asyncio
async def test_slim_path_surfaces_all_gathered_versions(session):
    """A collection gathering trials across task versions shows ALL of them,
    not just the newest gathered version (the single-effective-version rule
    does not apply to hand-curated collections)."""
    task, v1_id, v2_id, v1_trial, v2_trial, collection = (
        await _build_multi_version_collection(session)
    )

    responses = await list_experiment_slim_tasks(
        session, experiment_id=collection.id, org_id="org1"
    )

    by_task = {r.id: r for r in responses}
    assert task.id in by_task
    trial_ids = {t.id for t in (by_task[task.id].trials or [])}
    assert trial_ids == {v1_trial.id, v2_trial.id}, (
        "older-version gathered trial dropped from the collection grid"
    )
    # The reported version stays the newest gathered version.
    assert by_task[task.id].current_version_id == v2_id


@pytest.mark.asyncio
async def test_compact_path_surfaces_all_gathered_versions(session):
    """Same multi-version guarantee via the compact ``list_tasks_core`` path."""
    task, v1_id, v2_id, v1_trial, v2_trial, collection = (
        await _build_multi_version_collection(session)
    )

    responses = await list_tasks_core(
        session,
        experiment_id=collection.id,
        compact_trials=True,
        include_queue_info=False,
        include_worker_jobs=False,
        org_id="org1",
    )

    by_task = {r.id: r for r in responses}
    assert task.id in by_task
    trial_ids = {t.id for t in (by_task[task.id].trials or [])}
    assert trial_ids == {v1_trial.id, v2_trial.id}, (
        "older-version gathered trial hidden on compact experiment path"
    )
    assert by_task[task.id].current_version_id == v2_id


@pytest.mark.asyncio
async def test_normal_experiment_still_filters_to_effective_version(session):
    """No-regression control: a NORMAL experiment with trials on two versions
    keeps the single-effective-version rule (only the newest version's trials
    display) -- the no-filter rule is collection-only."""
    task = _task("normal-multiver-task")
    session.add(task)
    await session.flush()

    v1 = _version(task, 1)
    v2 = _version(task, 2)
    session.add_all([v1, v2])
    await session.flush()
    task.current_version_id = v2.id
    await session.flush()

    exp = _experiment("normal-multiver-exp")
    session.add(exp)
    await session.flush()

    v1_trial = _trial(task, exp, task_version_id=v1.id)
    v2_trial = _trial(task, exp, task_version_id=v2.id)
    session.add_all([v1_trial, v2_trial])
    await session.execute(
        task_experiments.insert().values(task_id=task.id, experiment_id=exp.id)
    )
    await session.flush()

    slim = await list_experiment_slim_tasks(
        session, experiment_id=exp.id, org_id="org1"
    )
    by_task = {r.id: r for r in slim}
    assert task.id in by_task
    assert [t.id for t in (by_task[task.id].trials or [])] == [v2_trial.id]
    assert by_task[task.id].current_version_id == v2.id

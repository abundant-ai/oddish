"""Per-experiment task-version pins.

A pin (``experiment_task_version_pins``) is a *display-only* override of the
version an experiment shows for one of its tasks. Resolution order inside an
experiment: pin -> derived-from-trials -> ``tasks.current_version_id``.

The pure-resolver tests below need no database; the ``session`` ones exercise
the real endpoints end to end (pin wins, stale pin falls back, DELETE reverts).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core import helpers
from oddish.core.endpoints.task_detail import (
    clear_experiment_task_default_version_core,
    get_task_detail_core,
    set_experiment_task_default_version_core,
    set_task_default_version_core,
)
from oddish.core.endpoints.tasks_query import (
    list_experiment_slim_tasks,
    list_experiment_task_shells_core,
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


# ---------------------------------------------------------------------------
# Pure resolver
# ---------------------------------------------------------------------------


def _scoped_task(task_id: str, *, current_version_id: str, trial_versions):
    return SimpleNamespace(
        id=task_id,
        current_version_id=current_version_id,
        trials=[
            SimpleNamespace(
                id=f"{task_id}-{i}",
                task_version_id=version_id,
                experiment_id=experiment_id,
                is_probe=False,
                superseded_by_trial_id=None,
            )
            for i, (version_id, experiment_id) in enumerate(trial_versions)
        ],
    )


def test_pin_wins_over_derived_version():
    task = _scoped_task(
        "task-1",
        current_version_id="task-1-v3",
        trial_versions=[("task-1-v1", "exp-a")],
    )

    assert (
        helpers.resolve_effective_version_id(
            task,
            experiment_context_id="exp-a",
            pins={("exp-a", "task-1"): "task-1-v2"},
        )
        == "task-1-v2"
    )


def test_pin_for_another_experiment_is_ignored():
    task = _scoped_task(
        "task-1",
        current_version_id="task-1-v3",
        trial_versions=[("task-1-v1", "exp-a")],
    )

    assert (
        helpers.resolve_effective_version_id(
            task,
            experiment_context_id="exp-a",
            pins={("exp-b", "task-1"): "task-1-v2"},
        )
        == "task-1-v1"
    )


def test_no_pins_keeps_derived_behavior():
    task = _scoped_task(
        "task-1",
        current_version_id="task-1-v3",
        trial_versions=[("task-1-v1", "exp-a"), ("task-1-v2", "exp-a")],
    )

    derived = helpers.resolve_effective_version_id(
        task, experiment_context_id="exp-a"
    )
    assert derived == "task-1-v2"
    assert (
        helpers.resolve_effective_version_id(
            task, experiment_context_id="exp-a", pins={}
        )
        == derived
    )
    assert (
        helpers.resolve_effective_version_id(
            task, experiment_context_id="exp-a", pins=None
        )
        == derived
    )


def test_pins_are_ignored_without_experiment_context():
    task = _scoped_task(
        "task-1",
        current_version_id="task-1-v3",
        trial_versions=[("task-1-v1", "exp-a")],
    )

    assert (
        helpers.resolve_effective_version_id(
            task, pins={("exp-a", "task-1"): "task-1-v2"}
        )
        == "task-1-v3"
    )


# ---------------------------------------------------------------------------
# DB-backed: real endpoints
# ---------------------------------------------------------------------------


def _task(name: str, *, org_id: str = "org1") -> TaskModel:
    return TaskModel(
        name=name, org_id=org_id, user="tester", task_path=f"s3://tasks/{name}"
    )


def _version(task: TaskModel, n: int) -> TaskVersionModel:
    return TaskVersionModel(
        id=f"{task.id}-v{n}", task_id=task.id, version=n, task_path=task.task_path
    )


def _trial(task, experiment, *, task_version_id: str) -> TrialModel:
    trial_id = generate_id()
    return TrialModel(
        id=trial_id,
        name=trial_id,
        task_id=task.id,
        task_version_id=task_version_id,
        experiment_id=experiment.id,
        org_id="org1",
        agent="codex",
        provider="openai",
        queue_key="openai/gpt-5.5",
        model="gpt-5.5",
        status=TrialStatus.SUCCESS,
        reward=1,
    )


async def _build_two_experiment_task(session):
    """One task on v1/v2/v3, default v3, with a trial on v1 in each experiment."""
    task = _task("pin-task")
    session.add(task)
    await session.flush()

    v1, v2, v3 = _version(task, 1), _version(task, 2), _version(task, 3)
    session.add_all([v1, v2, v3])
    await session.flush()

    task.current_version_id = v3.id
    exp_a = ExperimentModel(name="pin-exp-a", org_id="org1")
    exp_b = ExperimentModel(name="pin-exp-b", org_id="org1")
    session.add_all([exp_a, exp_b])
    await session.flush()

    await session.execute(
        task_experiments.insert().values(
            [
                {"task_id": task.id, "experiment_id": exp_a.id},
                {"task_id": task.id, "experiment_id": exp_b.id},
            ]
        )
    )
    session.add_all(
        [
            _trial(task, exp_a, task_version_id=v1.id),
            _trial(task, exp_b, task_version_id=v1.id),
            # v2 exists in exp_a too, so the derived pivot there is v2.
            _trial(task, exp_a, task_version_id=v2.id),
        ]
    )
    await session.flush()
    return task, (v1.id, v2.id, v3.id), exp_a, exp_b


async def _pivots(session, *, task_id: str, experiment_id: str):
    """(compact list, task shells, slim tasks) trial-version ids for one task.

    All three must agree or progressive loading flips the pivot mid-load.

    ``expire_all`` because these helpers write the experiment-scoped trial set
    back onto the identity-mapped task via ``set_committed_value``; a served
    request gets a fresh session, a test reusing one would otherwise resolve
    against the previous call's narrowed collection.
    """
    session.expire_all()
    compact = await list_tasks_core(
        session,
        experiment_id=experiment_id,
        compact_trials=True,
        include_queue_info=False,
        include_worker_jobs=False,
        org_id="org1",
    )
    shells = await list_experiment_task_shells_core(
        session, experiment_id=experiment_id, org_id="org1"
    )
    slim = await list_experiment_slim_tasks(
        session, experiment_id=experiment_id, org_id="org1"
    )
    return tuple(
        {response.id: response for response in group}[task_id]
        for group in (compact, shells, slim)
    )


@pytest.mark.asyncio
async def test_pin_overrides_derived_on_every_experiment_surface(session):
    task, (v1, v2, v3), exp_a, _exp_b = await _build_two_experiment_task(session)

    for response in await _pivots(session, task_id=task.id, experiment_id=exp_a.id):
        assert response.trial_version_id == v2, "derived pivot changed"

    await set_experiment_task_default_version_core(
        session, experiment_id=exp_a.id, task_id=task.id, version=1, org_id="org1"
    )
    await session.flush()

    for response in await _pivots(session, task_id=task.id, experiment_id=exp_a.id):
        assert response.trial_version_id == v1
        # AGENTS.md invariant: the reported default is always the task's own.
        assert response.current_version_id == v3
        assert response.current_version == 3
        assert response.trial_version == 1


@pytest.mark.asyncio
async def test_pin_is_scoped_to_one_experiment(session):
    task, (v1, v2, _v3), exp_a, exp_b = await _build_two_experiment_task(session)

    await set_experiment_task_default_version_core(
        session, experiment_id=exp_a.id, task_id=task.id, version=1, org_id="org1"
    )
    await session.flush()

    (compact_a, _, _) = await _pivots(
        session, task_id=task.id, experiment_id=exp_a.id
    )
    (compact_b, _, _) = await _pivots(
        session, task_id=task.id, experiment_id=exp_b.id
    )
    assert compact_a.trial_version_id == v1
    assert compact_b.trial_version_id == v1  # exp_b only ever ran v1
    assert compact_a.trial_version_id != v2


@pytest.mark.asyncio
async def test_stale_pinned_version_falls_back_to_derived(session):
    task, (v1, v2, _v3), exp_a, _exp_b = await _build_two_experiment_task(session)

    await set_experiment_task_default_version_core(
        session, experiment_id=exp_a.id, task_id=task.id, version=1, org_id="org1"
    )
    await session.flush()

    version_row = await session.get(TaskVersionModel, v1)
    await session.delete(version_row)
    await session.flush()

    for response in await _pivots(session, task_id=task.id, experiment_id=exp_a.id):
        assert response.trial_version_id == v2, "dangling pin was honored"


@pytest.mark.asyncio
async def test_clearing_a_pin_reverts_to_derived(session):
    task, (v1, v2, _v3), exp_a, _exp_b = await _build_two_experiment_task(session)

    await set_experiment_task_default_version_core(
        session, experiment_id=exp_a.id, task_id=task.id, version=1, org_id="org1"
    )
    await session.flush()
    (compact, _, _) = await _pivots(session, task_id=task.id, experiment_id=exp_a.id)
    assert compact.trial_version_id == v1

    await clear_experiment_task_default_version_core(
        session, experiment_id=exp_a.id, task_id=task.id, org_id="org1"
    )
    await session.flush()

    for response in await _pivots(session, task_id=task.id, experiment_id=exp_a.id):
        assert response.trial_version_id == v2

    # Re-pinning after a clear must not trip the partial unique index.
    await set_experiment_task_default_version_core(
        session, experiment_id=exp_a.id, task_id=task.id, version=1, org_id="org1"
    )
    await session.flush()
    (compact, _, _) = await _pivots(session, task_id=task.id, experiment_id=exp_a.id)
    assert compact.trial_version_id == v1


@pytest.mark.asyncio
async def test_pin_does_not_move_the_global_default(session):
    task, (v1, _v2, v3), exp_a, _exp_b = await _build_two_experiment_task(session)

    await set_experiment_task_default_version_core(
        session, experiment_id=exp_a.id, task_id=task.id, version=1, org_id="org1"
    )
    await session.flush()
    await session.refresh(task)
    assert task.current_version_id == v3

    detail = await get_task_detail_core(session, task_id=task.id, org_id="org1")
    assert detail.task.current_version_id == v3
    pins = {p.experiment_id: p.task_version for p in detail.experiment_version_pins}
    assert pins == {exp_a.id: 1}

    # The global setter still works and stays independent of the pin.
    await set_task_default_version_core(
        session, task_id=task.id, version=2, org_id="org1"
    )
    await session.flush()
    (compact, _, _) = await _pivots(session, task_id=task.id, experiment_id=exp_a.id)
    assert compact.current_version == 2
    assert compact.trial_version_id == v1


@pytest.mark.asyncio
async def test_pin_rejects_unknown_version_and_foreign_org(session):
    from fastapi import HTTPException

    task, _versions, exp_a, _exp_b = await _build_two_experiment_task(session)

    with pytest.raises(HTTPException) as unknown_version:
        await set_experiment_task_default_version_core(
            session,
            experiment_id=exp_a.id,
            task_id=task.id,
            version=99,
            org_id="org1",
        )
    assert unknown_version.value.status_code == 404

    with pytest.raises(HTTPException) as foreign_org:
        await set_experiment_task_default_version_core(
            session,
            experiment_id=exp_a.id,
            task_id=task.id,
            version=1,
            org_id="other-org",
        )
    assert foreign_org.value.status_code == 404

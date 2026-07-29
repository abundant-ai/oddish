from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.dashboard import _build_aggregates_for_experiment_ids
from oddish.core.endpoints.collections import create_trial_collection_core
from oddish.db.models import (
    ExperimentModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    generate_id,
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
    task: TaskModel,
    home_experiment: ExperimentModel,
    *,
    org_id: str = "org1",
    status: TrialStatus = TrialStatus.SUCCESS,
    reward: float | None = None,
    task_version_id: str | None = None,
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
        status=status,
        reward=reward,
        task_version_id=task_version_id,
        is_probe=is_probe,
    )


def _version(task: TaskModel, number: int) -> TaskVersionModel:
    return TaskVersionModel(
        id=f"{task.id}-v{number}",
        task_id=task.id,
        version=number,
        task_path=f"s3://tasks/{task.name}/v{number}",
    )


async def _trial_row(session, trial_agg, experiment_id: str):
    return (
        (
            await session.execute(
                select(trial_agg).where(trial_agg.c.experiment_id == experiment_id)
            )
        )
        .mappings()
        .one()
    )


async def _score_row(session, score_agg, experiment_id: str):
    return (
        (
            await session.execute(
                select(score_agg).where(score_agg.c.experiment_id == experiment_id)
            )
        )
        .mappings()
        .one()
    )


@pytest.mark.asyncio
async def test_collection_aggregate_counts_gathered_trials(session):
    task = _task("collection-agg-task-1")
    session.add(task)
    await session.flush()

    home = _experiment("collection-agg-home-1")
    session.add(home)
    await session.flush()

    t1 = _trial(task, home, org_id="org1", status=TrialStatus.SUCCESS, reward=1.0)
    t2 = _trial(task, home, org_id="org1", status=TrialStatus.SUCCESS, reward=0.0)
    session.add_all([t1, t2])
    await session.flush()

    coll = await create_trial_collection_core(
        session, name="c", trial_ids=[t1.id, t2.id], org_id="org1"
    )
    await session.flush()

    task_agg, trial_agg, score_agg = _build_aggregates_for_experiment_ids(
        [coll.id], org_id="org1"
    )

    trial_row = (
        (
            await session.execute(
                select(trial_agg).where(trial_agg.c.experiment_id == coll.id)
            )
        )
        .mappings()
        .one()
    )
    assert trial_row["total_trials"] == 2
    assert trial_row["completed_trials"] == 2

    score_row = (
        (
            await session.execute(
                select(score_agg).where(score_agg.c.experiment_id == coll.id)
            )
        )
        .mappings()
        .one()
    )
    assert score_row["avg_score"] is not None
    assert score_row["avg_score"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_normal_experiment_aggregate_unaffected_by_membership_union(session):
    """Guards the byte-for-byte-reproduction claim for non-collection experiments."""
    task = _task("collection-agg-task-2")
    session.add(task)
    await session.flush()

    home = _experiment("collection-agg-home-2")
    session.add(home)
    await session.flush()

    t1 = _trial(task, home, org_id="org1", status=TrialStatus.SUCCESS, reward=1.0)
    t2 = _trial(task, home, org_id="org1", status=TrialStatus.FAILED, reward=None)
    session.add_all([t1, t2])
    await session.flush()

    task_agg, trial_agg, score_agg = _build_aggregates_for_experiment_ids(
        [home.id], org_id="org1"
    )

    trial_row = (
        (
            await session.execute(
                select(trial_agg).where(trial_agg.c.experiment_id == home.id)
            )
        )
        .mappings()
        .one()
    )
    assert trial_row["total_trials"] == 2
    assert trial_row["completed_trials"] == 1
    assert trial_row["failed_trials"] == 1


@pytest.mark.asyncio
async def test_reward_aggregate_excludes_probes(session):
    """Probes are not experiment trials -- every per-task path drops them."""
    task = _task("dash-probe-task")
    session.add(task)
    await session.flush()

    home = _experiment("dash-probe-home")
    session.add(home)
    await session.flush()

    session.add_all(
        [
            _trial(task, home, reward=0.0),
            _trial(task, home, reward=1.0, is_probe=True),
        ]
    )
    await session.flush()

    _, trial_agg, score_agg = _build_aggregates_for_experiment_ids(
        [home.id], org_id="org1"
    )

    trial_row = await _trial_row(session, trial_agg, home.id)
    assert trial_row["reward_total"] == 1
    assert trial_row["reward_success"] == 0
    assert trial_row["total_trials"] == 1

    score_row = await _score_row(session, score_agg, home.id)
    assert score_row["avg_score"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_reward_aggregate_scoped_to_effective_version(session):
    """Reward counts only the task's effective version, matching the grid."""
    task = _task("dash-version-task")
    session.add(task)
    await session.flush()

    v1, v2 = _version(task, 1), _version(task, 2)
    session.add_all([v1, v2])
    await session.flush()
    task.current_version_id = v2.id

    home = _experiment("dash-version-home")
    session.add(home)
    await session.flush()

    session.add_all(
        [
            _trial(task, home, reward=0.0, task_version_id=v1.id),
            _trial(task, home, reward=0.0, task_version_id=v1.id),
            _trial(task, home, reward=1.0, task_version_id=v2.id),
        ]
    )
    await session.flush()

    _, trial_agg, score_agg = _build_aggregates_for_experiment_ids(
        [home.id], org_id="org1"
    )

    trial_row = await _trial_row(session, trial_agg, home.id)
    assert trial_row["reward_total"] == 1
    assert trial_row["reward_success"] == 1
    assert float(trial_row["reward_sum"]) == pytest.approx(1.0)

    score_row = await _score_row(session, score_agg, home.id)
    assert score_row["avg_score"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_status_counters_stay_unscoped_by_version(session):
    """Deliberate: narrowing ``active_trials`` would report a running
    experiment as finished the moment its task gets a new version."""
    task = _task("dash-counter-task")
    session.add(task)
    await session.flush()

    v1, v2 = _version(task, 1), _version(task, 2)
    session.add_all([v1, v2])
    await session.flush()
    task.current_version_id = v2.id

    home = _experiment("dash-counter-home")
    session.add(home)
    await session.flush()

    session.add_all(
        [
            _trial(task, home, status=TrialStatus.RUNNING, task_version_id=v1.id),
            _trial(task, home, reward=1.0, task_version_id=v2.id),
        ]
    )
    await session.flush()

    _, trial_agg, _ = _build_aggregates_for_experiment_ids([home.id], org_id="org1")

    trial_row = await _trial_row(session, trial_agg, home.id)
    assert trial_row["total_trials"] == 2
    assert trial_row["active_trials"] == 1


@pytest.mark.asyncio
async def test_reward_aggregate_keeps_trials_with_no_version(session):
    """Fallback guard: a task whose trials carry no ``task_version_id`` has no
    effective version, so all of its trials still count."""
    task = _task("dash-nullver-task")
    session.add(task)
    await session.flush()

    home = _experiment("dash-nullver-home")
    session.add(home)
    await session.flush()

    session.add_all(
        [
            _trial(task, home, reward=1.0),
            _trial(task, home, reward=0.0),
        ]
    )
    await session.flush()

    _, trial_agg, _ = _build_aggregates_for_experiment_ids([home.id], org_id="org1")

    trial_row = await _trial_row(session, trial_agg, home.id)
    assert trial_row["reward_total"] == 2
    assert trial_row["reward_success"] == 1

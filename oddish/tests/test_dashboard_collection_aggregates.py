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

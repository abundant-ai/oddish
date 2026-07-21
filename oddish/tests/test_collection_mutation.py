from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import insert, select, update

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.endpoints.collections import (
    create_trial_collection_core,
    resolve_collection_sources,
)
from oddish.db import experiment_trials, task_experiments, utcnow
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


def _experiment(name: str, *, org_id: str = "org1", is_collection: bool = False):
    return ExperimentModel(name=name, org_id=org_id, is_collection=is_collection)


def _trial(
    task: TaskModel,
    home: ExperimentModel,
    *,
    org_id: str = "org1",
    status: TrialStatus = TrialStatus.SUCCESS,
    is_probe: bool = False,
    task_version_id: str | None = None,
) -> TrialModel:
    trial_id = generate_id()
    return TrialModel(
        id=trial_id,
        name=trial_id,
        task_id=task.id,
        task_version_id=task_version_id,
        experiment_id=home.id,
        org_id=org_id,
        agent="codex",
        provider="openai",
        queue_key="openai/gpt-5.5",
        model="gpt-5.5",
        status=status,
        is_probe=is_probe,
    )


@pytest.mark.asyncio
async def test_from_experiment_ids_includes_gathered_trials(session):
    """A source experiment's GATHERED trials must come across, not just the
    trials whose home experiment_id points at it."""
    task = _task("from-exp-task")
    session.add(task)
    await session.flush()

    home = _experiment("from-exp-home")
    source = _experiment("from-exp-source", is_collection=True)
    session.add_all([home, source])
    await session.flush()

    # `home_trial` lives in `source` the normal way; `gathered` only reaches it
    # through an experiment_trials row.
    home_trial = _trial(task, source)
    gathered = _trial(task, home)
    session.add_all([home_trial, gathered])
    await session.flush()

    await session.execute(
        insert(experiment_trials).values(
            experiment_id=source.id, trial_id=gathered.id
        )
    )

    trials, _ = await resolve_collection_sources(
        session, from_experiment_ids=[source.id], org_id="org1"
    )

    assert {t.id for t in trials} == {home_trial.id, gathered.id}


@pytest.mark.asyncio
async def test_from_experiment_ids_filters_probe_and_nonterminal(session):
    task = _task("from-exp-filter-task")
    session.add(task)
    await session.flush()

    source = _experiment("from-exp-filter-source")
    session.add(source)
    await session.flush()

    keep = _trial(task, source, status=TrialStatus.FAILED)
    probe = _trial(task, source, is_probe=True)
    running = _trial(task, source, status=TrialStatus.RUNNING)
    session.add_all([keep, probe, running])
    await session.flush()

    trials, _ = await resolve_collection_sources(
        session, from_experiment_ids=[source.id], org_id="org1"
    )

    assert {t.id for t in trials} == {keep.id}


@pytest.mark.asyncio
async def test_from_experiment_id_cross_org_is_404(session):
    other = _experiment("from-exp-other-org", org_id="org2")
    session.add(other)
    await session.flush()

    with pytest.raises(HTTPException) as exc:
        await resolve_collection_sources(
            session, from_experiment_ids=[other.id], org_id="org1"
        )
    assert exc.value.status_code == 404

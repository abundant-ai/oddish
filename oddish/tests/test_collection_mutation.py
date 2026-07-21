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


async def _make_collection(session, *, trials, name="coll") -> str:
    resp = await create_trial_collection_core(
        session, name=name, trial_ids=[t.id for t in trials], org_id="org1"
    )
    await session.flush()
    return resp.id


async def _live_ids(session, experiment_id: str) -> set[str]:
    rows = (
        (
            await session.execute(
                select(experiment_trials.c.trial_id).where(
                    experiment_trials.c.experiment_id == experiment_id,
                    experiment_trials.c.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


@pytest.mark.asyncio
async def test_add_links_new_trial(session):
    from oddish.core.endpoints.collections import add_to_collection_core

    task = _task("add-task")
    session.add(task)
    await session.flush()
    home = _experiment("add-home")
    session.add(home)
    await session.flush()
    t1, t2 = _trial(task, home), _trial(task, home)
    session.add_all([t1, t2])
    await session.flush()

    coll_id = await _make_collection(session, trials=[t1])

    resp = await add_to_collection_core(
        session, experiment_id=coll_id, trial_ids=[t2.id], org_id="org1"
    )
    await session.flush()

    assert resp.trials_added == 1
    assert resp.trials_total == 2
    assert await _live_ids(session, coll_id) == {t1.id, t2.id}


@pytest.mark.asyncio
async def test_add_resurrects_soft_deleted_membership(session):
    """PROVE-RED: a plain on_conflict_do_nothing leaves the row tombstoned."""
    from oddish.core.endpoints.collections import add_to_collection_core

    task = _task("resurrect-task")
    session.add(task)
    await session.flush()
    home = _experiment("resurrect-home")
    session.add(home)
    await session.flush()
    t1, t2 = _trial(task, home), _trial(task, home)
    session.add_all([t1, t2])
    await session.flush()

    coll_id = await _make_collection(session, trials=[t1, t2])
    await session.execute(
        update(experiment_trials)
        .where(
            experiment_trials.c.experiment_id == coll_id,
            experiment_trials.c.trial_id == t2.id,
        )
        .values(deleted_at=utcnow())
    )
    assert await _live_ids(session, coll_id) == {t1.id}

    resp = await add_to_collection_core(
        session, experiment_id=coll_id, trial_ids=[t2.id], org_id="org1"
    )
    await session.flush()

    assert resp.trials_added == 1
    assert await _live_ids(session, coll_id) == {t1.id, t2.id}


@pytest.mark.asyncio
async def test_add_is_idempotent(session):
    from oddish.core.endpoints.collections import add_to_collection_core

    task = _task("idempotent-task")
    session.add(task)
    await session.flush()
    home = _experiment("idempotent-home")
    session.add(home)
    await session.flush()
    t1 = _trial(task, home)
    session.add(t1)
    await session.flush()

    coll_id = await _make_collection(session, trials=[t1])

    resp = await add_to_collection_core(
        session, experiment_id=coll_id, trial_ids=[t1.id], org_id="org1"
    )
    await session.flush()

    assert resp.trials_added == 0
    assert resp.trials_total == 1
    assert await _live_ids(session, coll_id) == {t1.id}


@pytest.mark.asyncio
async def test_add_links_a_newly_represented_task(session):
    from oddish.core.endpoints.collections import add_to_collection_core

    task_a, task_b = _task("add-link-a"), _task("add-link-b")
    session.add_all([task_a, task_b])
    await session.flush()
    home = _experiment("add-link-home")
    session.add(home)
    await session.flush()
    ta, tb = _trial(task_a, home), _trial(task_b, home)
    session.add_all([ta, tb])
    await session.flush()

    coll_id = await _make_collection(session, trials=[ta])

    resp = await add_to_collection_core(
        session, experiment_id=coll_id, trial_ids=[tb.id], org_id="org1"
    )
    await session.flush()

    linked = (
        (
            await session.execute(
                select(task_experiments.c.task_id).where(
                    task_experiments.c.experiment_id == coll_id,
                    task_experiments.c.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert set(linked) == {task_a.id, task_b.id}
    assert resp.tasks_linked == 1


@pytest.mark.asyncio
async def test_add_rejects_non_collection_experiment(session):
    from oddish.core.endpoints.collections import add_to_collection_core

    task = _task("non-coll-task")
    session.add(task)
    await session.flush()
    real = _experiment("a-real-experiment")
    session.add(real)
    await session.flush()
    t1 = _trial(task, real)
    session.add(t1)
    await session.flush()

    with pytest.raises(HTTPException) as exc:
        await add_to_collection_core(
            session, experiment_id=real.id, trial_ids=[t1.id], org_id="org1"
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_add_to_cross_org_collection_is_404(session):
    from oddish.core.endpoints.collections import add_to_collection_core

    other = _experiment("cross-org-coll", org_id="org2", is_collection=True)
    session.add(other)
    await session.flush()

    with pytest.raises(HTTPException) as exc:
        await add_to_collection_core(
            session, experiment_id=other.id, trial_ids=["whatever"], org_id="org1"
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_add_with_no_sources_is_400(session):
    from oddish.core.endpoints.collections import add_to_collection_core

    task = _task("no-sources-task")
    session.add(task)
    await session.flush()
    home = _experiment("no-sources-home")
    session.add(home)
    await session.flush()
    t1 = _trial(task, home)
    session.add(t1)
    await session.flush()

    coll_id = await _make_collection(session, trials=[t1])

    with pytest.raises(HTTPException) as exc:
        await add_to_collection_core(session, experiment_id=coll_id, org_id="org1")
    assert exc.value.status_code == 400

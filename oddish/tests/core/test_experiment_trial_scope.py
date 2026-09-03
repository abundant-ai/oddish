"""``experiment_trial_scope`` -- one experiment's trials as a FROM clause.

The experiment page, its summary, and its cost tiles read every trial an
experiment owns. Filtering the whole ``trials`` table with
``experiment_id = X OR id IN (gathered)`` cannot use an index, so these tests
pin the replacement: a ``UNION ALL`` of the homed rows and the gathered rows,
each an index seek, with the same membership semantics as
``trial_in_experiment``:

* homed and gathered rows appear once each, even when a trial is both;
* soft-deleted membership rows drop out; soft-deleted trials drop out of
  reads and stay in spend (``include_deleted``);
* a combine copy is hidden only while its source is also a member, for
  readable and hashed idempotency keys alike.

Then the endpoints on top: ``/open`` totals, ``/trial-page`` order and
pagination, ``/focus`` addressing a gathered trial, and cost totals.

Uses the rollback-per-test ``session`` fixture against the local Postgres.
"""

from __future__ import annotations

import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import insert, select

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from oddish.core.endpoints.deletion import _combine_idempotency_key  # noqa: E402
from oddish.core.endpoints.experiment_cost import (  # noqa: E402
    get_experiment_cost_totals,
)
from oddish.core.endpoints.experiment_page import (  # noqa: E402
    get_experiment_focus_core,
    get_experiment_open_core,
    get_experiment_trial_page_core,
)
from oddish.core.experiment_membership import experiment_trial_scope  # noqa: E402
from oddish.db.models import (  # noqa: E402
    ExperimentModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    experiment_trials,
    generate_id,
    task_experiments,
    utcnow,
)

_ORG = f"scope-org-{uuid.uuid4().hex[:8]}"


async def _experiment(session, slug: str) -> ExperimentModel:
    experiment = ExperimentModel(
        name=f"scope-exp-{slug}-{_ORG}", org_id=_ORG, last_activity_at=utcnow()
    )
    session.add(experiment)
    await session.flush()
    return experiment


async def _task(session, slug: str, *, versions: int = 0) -> TaskModel:
    task = TaskModel(
        name=f"scope-task-{slug}-{_ORG}",
        org_id=_ORG,
        user="tester",
        task_path=f"s3://tasks/scope-{slug}",
    )
    session.add(task)
    await session.flush()
    for number in range(1, versions + 1):
        session.add(
            TaskVersionModel(
                id=f"{task.id}-v{number}",
                task_id=task.id,
                version=number,
                task_path=task.task_path,
            )
        )
    if versions:
        await session.flush()
    return task


def _trial(
    task: TaskModel,
    experiment: ExperimentModel,
    *,
    trial_id: str | None = None,
    task_version_id: str | None = None,
    status: TrialStatus = TrialStatus.SUCCESS,
    reward: float | None = 1.0,
    cost_usd: float | None = None,
    idempotency_key: str | None = None,
    is_probe: bool = False,
    superseded_by_trial_id: str | None = None,
    age_seconds: int = 0,
) -> TrialModel:
    trial_id = trial_id or generate_id()
    return TrialModel(
        id=trial_id,
        name=trial_id,
        task_id=task.id,
        task_version_id=task_version_id,
        experiment_id=experiment.id,
        org_id=_ORG,
        agent="codex",
        provider="openai",
        queue_key="openai/gpt-5.5",
        model="gpt-5.5",
        status=status,
        reward=reward if status == TrialStatus.SUCCESS else None,
        cost_usd=cost_usd,
        idempotency_key=idempotency_key,
        is_probe=is_probe,
        superseded_by_trial_id=superseded_by_trial_id,
        created_at=utcnow() - timedelta(seconds=age_seconds),
    )


async def _gather(session, experiment: ExperimentModel, *trials: TrialModel) -> None:
    await session.execute(
        insert(experiment_trials).values(
            [{"experiment_id": experiment.id, "trial_id": trial.id} for trial in trials]
        )
    )
    await session.flush()


async def _link_task(session, experiment: ExperimentModel, task: TaskModel) -> None:
    await session.execute(
        insert(task_experiments).values(
            [{"experiment_id": experiment.id, "task_id": task.id}]
        )
    )
    await session.flush()


async def _member_ids(session, experiment: ExperimentModel, **options) -> set[str]:
    scope = experiment_trial_scope(experiment.id, org_id=_ORG)
    query = select(scope.trials.id).where(*scope.member_predicates())
    if options:
        query = query.execution_options(**options)
    return set((await session.execute(query)).scalars().all())


@pytest.mark.asyncio
async def test_scope_reads_homed_and_gathered_trials_once(session):
    task = await _task(session, "union")
    home = await _experiment(session, "home")
    collection = await _experiment(session, "collection")

    borrowed = _trial(task, home)
    native = _trial(task, collection)
    unrelated = _trial(task, home)
    session.add_all([borrowed, native, unrelated])
    await session.flush()
    # ``native`` is both homed and gathered: it must not appear twice.
    await _gather(session, collection, borrowed, native)

    assert await _member_ids(session, collection) == {borrowed.id, native.id}
    assert await _member_ids(session, home) == {borrowed.id, unrelated.id}

    rows = (
        (
            await session.execute(
                select(experiment_trial_scope(collection.id, org_id=_ORG).trials.id)
            )
        )
        .scalars()
        .all()
    )
    assert sorted(rows) == sorted([borrowed.id, native.id])


@pytest.mark.asyncio
async def test_scope_honours_membership_and_trial_soft_deletes(session):
    task = await _task(session, "deletes")
    home = await _experiment(session, "home")
    collection = await _experiment(session, "collection")

    removed_from_collection = _trial(task, home)
    deleted_trial = _trial(task, home, cost_usd=2.0)
    kept = _trial(task, home)
    session.add_all([removed_from_collection, deleted_trial, kept])
    await session.flush()
    await _gather(session, collection, removed_from_collection, deleted_trial, kept)
    await session.execute(
        experiment_trials.update()
        .where(experiment_trials.c.trial_id == removed_from_collection.id)
        .values(deleted_at=utcnow())
    )
    deleted_trial.deleted_at = utcnow()
    await session.flush()

    # Reads drop the soft-deleted trial and the removed membership row.
    assert await _member_ids(session, collection) == {kept.id}
    # Spend keeps the soft-deleted trial (it was still run and billed) but
    # never resurrects a removed membership.
    assert await _member_ids(session, collection, include_deleted=True) == {
        kept.id,
        deleted_trial.id,
    }
    totals = await get_experiment_cost_totals(
        session, experiment_id=collection.id, org_id=_ORG
    )
    assert totals.cost_trial_count == 1
    assert totals.cost_usd == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_scope_hides_combine_copies_only_while_their_source_is_a_member(
    session,
):
    task = await _task(session, "combine")
    home = await _experiment(session, "home")
    result = await _experiment(session, "result")
    other_result = await _experiment(session, "other-result")

    source = _trial(task, home)
    # A 72-character id pushes the readable key past 64 characters, so the
    # SQL twin must reproduce the hashed form of the key.
    long_source = _trial(task, home, trial_id="s" * 72)
    session.add_all([source, long_source])
    await session.flush()

    copy = _trial(
        task, result, idempotency_key=_combine_idempotency_key(result.id, source.id)
    )
    hashed_copy = _trial(
        task,
        result,
        idempotency_key=_combine_idempotency_key(result.id, long_source.id),
    )
    assert hashed_copy.idempotency_key != f"combine:{result.id}:{long_source.id}"
    orphan_copy = _trial(
        task,
        other_result,
        idempotency_key=_combine_idempotency_key(other_result.id, source.id),
    )
    session.add_all([copy, hashed_copy, orphan_copy])
    await session.flush()
    # ``result`` gathers both sources, so both copies duplicate a member and
    # are hidden. ``other_result`` gathers nothing, so its copy is the only
    # record of that execution there and stays.
    await _gather(session, result, source, long_source)

    assert await _member_ids(session, result) == {source.id, long_source.id}
    assert await _member_ids(session, other_result) == {orphan_copy.id}

    totals = await get_experiment_cost_totals(
        session, experiment_id=result.id, org_id=_ORG
    )
    assert totals.total_trials == 2

    # Once its source is soft-deleted, the copy is the only visible record of
    # that execution, so reads show it again; spend still sees the source (it
    # opts out of soft-delete filtering) and keeps hiding the copy.
    long_source.deleted_at = utcnow()
    await session.flush()
    assert await _member_ids(session, result) == {source.id, hashed_copy.id}
    assert await _member_ids(session, result, include_deleted=True) == {
        source.id,
        long_source.id,
    }


@pytest.mark.asyncio
async def test_open_summary_and_trial_page_follow_the_effective_version(session):
    task = await _task(session, "versions", versions=2)
    experiment = await _experiment(session, "versions")
    await _link_task(session, experiment, task)
    v1, v2 = f"{task.id}-v1", f"{task.id}-v2"
    # The task's default version is v1 and a visible trial represents it, so
    # v1 is the effective version even though v2 is newer.
    task.current_version_id = v1

    newest = _trial(task, experiment, task_version_id=v1, reward=1.0, age_seconds=1)
    older = _trial(task, experiment, task_version_id=v1, reward=0.0, age_seconds=2)
    oldest = _trial(
        task, experiment, task_version_id=v1, status=TrialStatus.RUNNING, age_seconds=3
    )
    on_v2 = _trial(task, experiment, task_version_id=v2, reward=1.0, age_seconds=4)
    probe = _trial(task, experiment, task_version_id=v1, is_probe=True, age_seconds=5)
    session.add_all([newest, older, oldest, on_v2, probe])
    await session.flush()
    superseded = _trial(
        task,
        experiment,
        task_version_id=v1,
        reward=1.0,
        superseded_by_trial_id=newest.id,
        age_seconds=6,
    )
    session.add(superseded)
    await session.flush()

    response = await get_experiment_open_core(
        session, experiment_id=experiment.id, org_id=_ORG
    )
    assert response.has_active_trials is True
    assert response.summary is not None
    assert response.summary.task_count == 1
    assert response.summary.trial_count == 3
    assert response.summary.completed == 2
    assert response.summary.active == 1
    assert response.summary.pass_count == 1
    assert response.summary.fail_count == 1
    assert response.summary.average_score == pytest.approx(0.5)
    [task_row] = response.tasks
    assert task_row.trial_version_id == v1
    assert task_row.total == 3

    first = await get_experiment_trial_page_core(
        session, experiment_id=experiment.id, org_id=_ORG, limit=2
    )
    assert [trial.id for trial in first.trials] == [newest.id, older.id]
    assert first.next_trial_id == older.id
    second = await get_experiment_trial_page_core(
        session,
        experiment_id=experiment.id,
        org_id=_ORG,
        limit=2,
        before_created_at=first.next_created_at,
        before_trial_id=first.next_trial_id,
    )
    assert [trial.id for trial in second.trials] == [oldest.id]
    assert second.next_trial_id is None


@pytest.mark.asyncio
async def test_focus_addresses_a_gathered_trial_by_id(session):
    task = await _task(session, "focus")
    home = await _experiment(session, "home")
    collection = await _experiment(session, "collection")
    await _link_task(session, collection, task)

    borrowed = _trial(task, home, reward=1.0)
    session.add(borrowed)
    await session.flush()
    await _gather(session, collection, borrowed)

    response = await get_experiment_focus_core(
        session, experiment_id=collection.id, org_id=_ORG, trial_id=borrowed.id
    )
    assert response.trial is not None
    assert response.trial.id == borrowed.id
    assert response.trial.experiment_id == home.id
    assert response.task.id == task.id
    assert response.task.total == 1

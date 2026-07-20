"""Tests for api.services.summary_dump."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models  # noqa: F401  registers cloud tables on the shared Base
from api.services.summary_dump import resolve_cohort, validate_scope
from models import OrganizationModel
from oddish.db.models import (
    Base,
    ExperimentModel,
    TaskModel,
    TrialModel,
    TrialOrigin,
    TrialStatus,
)


def test_validate_scope_rejects_no_scope():
    with pytest.raises(ValueError, match="exactly one"):
        validate_scope(trials=None, task=None, experiment=None)


def test_validate_scope_rejects_two_scopes():
    with pytest.raises(ValueError, match="exactly one"):
        validate_scope(trials=["tr_a"], task="my-task", experiment=None)


def test_validate_scope_accepts_single_scope():
    validate_scope(trials=["tr_a"], task=None, experiment=None)
    validate_scope(trials=None, task="my-task", experiment=None)
    validate_scope(trials=None, task=None, experiment="exp_1")


def test_validate_scope_rejects_empty_trial_list():
    with pytest.raises(ValueError, match="exactly one"):
        validate_scope(trials=[], task=None, experiment=None)


def _candidate(trial_id: str, *, has_trajectory=True, agent="claude-code", finished_at=object()):
    return SimpleNamespace(
        id=trial_id, has_trajectory=has_trajectory, agent=agent, finished_at=finished_at,
    )


def test_filter_fetchable_keeps_only_trials_with_a_trajectory():
    from api.services.summary_dump import filter_fetchable

    rows = [
        _candidate("tr_a"),
        _candidate("tr_b", has_trajectory=False),
        _candidate("tr_c", has_trajectory=False, agent="grok-build"),
        _candidate("tr_d", has_trajectory=False, agent="grok-build", finished_at=None),
    ]
    assert [t.id for t in filter_fetchable(rows)] == ["tr_a", "tr_c"]


def test_filter_fetchable_applies_limit_after_filtering():
    from api.services.summary_dump import filter_fetchable

    rows = [_candidate("tr_a", has_trajectory=False), _candidate("tr_b"), _candidate("tr_c")]
    assert [t.id for t in filter_fetchable(rows, limit=1)] == ["tr_b"]


# ---------------------------------------------------------------------------
# resolve_cohort -- DB-backed tests
#
# Same schema-reset pattern as test_task_affiliated_experiments.py: wipe and
# rebuild the public schema per test against ODDISH_DATABASE_URL, so tests
# don't need unique-suffixed ids and can't see each other's rows.
# ---------------------------------------------------------------------------

URL = os.environ.get("ODDISH_DATABASE_URL")
requires_db = pytest.mark.skipif(not URL, reason="ODDISH_DATABASE_URL not set")


@asynccontextmanager
async def _fresh_db():
    engine = create_async_engine(URL)
    try:
        async with engine.begin() as c:
            await c.execute(text("drop schema public cascade"))
            await c.execute(text("create schema public"))
            await c.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _trial(id_, *, task_id, experiment_id, org_id="org1", **overrides):
    defaults = dict(
        name=f"{id_}-name",
        agent="claude-code",
        provider="anthropic",
        model="anthropic/claude-sonnet-4-6",
        queue_key="test-summary-dump",
        status=TrialStatus.SUCCESS,
        origin=TrialOrigin.ODDISH,
        is_probe=False,
        has_trajectory=True,
    )
    defaults.update(overrides)
    return TrialModel(
        id=id_, task_id=task_id, experiment_id=experiment_id, org_id=org_id, **defaults
    )


async def _seed_org_experiment_task(session, *, task_id="task1", task_name="task-1"):
    session.add(OrganizationModel(id="org1", name="Org", slug="org"))
    session.add(ExperimentModel(id="exp1", name="Exp", org_id="org1"))
    session.add(
        TaskModel(id=task_id, name=task_name, user="u", task_path="/p", org_id="org1")
    )
    await session.flush()


@requires_db
@pytest.mark.asyncio
async def test_resolve_cohort_experiment_scope_excludes_probes():
    async with _fresh_db() as maker:
        async with maker() as session:
            await _seed_org_experiment_task(session)
            session.add(_trial("tr-real", task_id="task1", experiment_id="exp1", is_probe=False))
            session.add(_trial("tr-probe", task_id="task1", experiment_id="exp1", is_probe=True))
            await session.commit()

        async with maker() as session:
            result = await resolve_cohort(session, experiment="exp1")

    assert [t.id for t in result] == ["tr-real"]


@requires_db
@pytest.mark.asyncio
async def test_resolve_cohort_task_scope_resolves_by_name_via_join():
    async with _fresh_db() as maker:
        async with maker() as session:
            session.add(OrganizationModel(id="org1", name="Org", slug="org"))
            session.add(ExperimentModel(id="exp1", name="Exp", org_id="org1"))
            session.add(
                TaskModel(id="task-x", name="task-x-name", user="u", task_path="/p", org_id="org1")
            )
            session.add(
                TaskModel(id="task-y", name="task-y-name", user="u", task_path="/p", org_id="org1")
            )
            await session.flush()
            session.add(_trial("tr-x", task_id="task-x", experiment_id="exp1"))
            session.add(_trial("tr-y", task_id="task-y", experiment_id="exp1"))
            await session.commit()

        async with maker() as session:
            result = await resolve_cohort(session, task="task-x-name")

    assert [t.id for t in result] == ["tr-x"]
    # Task 3 reads trials after the session (and engine) is gone; `.task` must
    # already be populated via the mapper-level selectin eager load, not lazy.
    assert result[0].task.name == "task-x-name"


@requires_db
@pytest.mark.asyncio
async def test_resolve_cohort_orders_by_trial_id_ascending():
    async with _fresh_db() as maker:
        async with maker() as session:
            await _seed_org_experiment_task(session)
            for tid in ("trial-c", "trial-a", "trial-b"):  # insert out of id order
                session.add(_trial(tid, task_id="task1", experiment_id="exp1"))
            await session.commit()

        async with maker() as session:
            result = await resolve_cohort(session, experiment="exp1")

    assert [t.id for t in result] == ["trial-a", "trial-b", "trial-c"]


@requires_db
@pytest.mark.asyncio
async def test_resolve_cohort_explicit_trials_preserve_order_and_drop_missing():
    async with _fresh_db() as maker:
        async with maker() as session:
            await _seed_org_experiment_task(session)
            session.add(_trial("trial-x", task_id="task1", experiment_id="exp1"))
            session.add(_trial("trial-y", task_id="task1", experiment_id="exp1"))
            await session.commit()

        async with maker() as session:
            result = await resolve_cohort(
                session, trials=["trial-y", "trial-missing", "trial-x"]
            )

    assert [t.id for t in result] == ["trial-y", "trial-x"]


@requires_db
@pytest.mark.asyncio
async def test_resolve_cohort_limit_applies_after_fetchable_filter():
    async with _fresh_db() as maker:
        async with maker() as session:
            await _seed_org_experiment_task(session)
            # "trial-1-bad" sorts first by id but isn't fetchable; a SQL LIMIT
            # applied before filtering would return zero fetchable rows here.
            session.add(
                _trial("trial-1-bad", task_id="task1", experiment_id="exp1", has_trajectory=False)
            )
            session.add(
                _trial("trial-2-good", task_id="task1", experiment_id="exp1", has_trajectory=True)
            )
            await session.commit()

        async with maker() as session:
            result = await resolve_cohort(session, experiment="exp1", limit=1)

    assert [t.id for t in result] == ["trial-2-good"]

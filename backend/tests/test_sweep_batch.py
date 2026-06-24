"""Batch task-sweep submission: best-effort, per-item, savepoint-isolated.

``create_task_sweep_batch_core`` accepts N sweep submissions and creates each
inside its own SAVEPOINT (``session.begin_nested()``). A failure in one item
rolls back only that item: sibling items -- and the rows they already inserted
-- survive, and the session stays usable for the items that follow the bad one.
The handler returns a per-item status array aligned to the request order.

Runs against an empty Postgres via ``ODDISH_DATABASE_URL``; skips otherwise.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import (  # type: ignore[attr-defined]
    async_sessionmaker,
    create_async_engine,
)

import models  # noqa: F401  registers cloud tables on the shared Base
from oddish.core.endpoints import create_task_sweep_batch_core
from oddish.db import TrialModel
from oddish.db.models import Base
from oddish.schemas import AgentModelPair, TaskSweepSubmission

URL = os.environ.get("ODDISH_DATABASE_URL")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not URL, reason="ODDISH_DATABASE_URL not set"),
]

ORG_ID = "org-batch"


async def _reset_schema_and_seed_tasks(engine, task_ids: list[str]) -> None:
    """Drop/recreate the public schema and seed one org + the given tasks.

    Each task is an append target: it exists with no trials yet, so the sweep
    runs in append mode and auto-creates an experiment for it.
    """
    async with engine.begin() as c:
        await c.execute(text("drop schema public cascade"))
        await c.execute(text("create schema public"))
        await c.run_sync(Base.metadata.create_all)
        # ``create_all`` builds tables from the ORM models but not the partial
        # unique indexes defined only in Alembic migrations. The sweep path
        # enqueues a coalescing TAG_PROJECT worker job whose ``ON CONFLICT``
        # targets this index, so it must exist for the insert to resolve.
        await c.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_worker_jobs_tag_project_active "
                "ON worker_jobs (kind, subject_table, subject_id) "
                "WHERE kind = 'TAG_PROJECT' "
                "AND status IN ('QUEUED', 'RETRYING') "
                "AND subject_id IS NOT NULL"
            )
        )
        await c.execute(
            text(
                "insert into organizations "
                "(id,name,slug,plan,settings,is_active,created_at,updated_at) "
                "values (:org,'Batch','batch','free','{}'::jsonb,true,now(),now())"
            ),
            {"org": ORG_ID},
        )
        for task_id in task_ids:
            await c.execute(
                text(
                    "insert into tasks "
                    '(id,name,org_id,"user",priority,status,task_path,tags,'
                    "run_analysis,run_probe,created_at,updated_at) "
                    "values (:id,:id,:org,'u','LOW','COMPLETED','p',"
                    "'{}'::jsonb,false,false,now(),now())"
                ),
                {"id": task_id, "org": ORG_ID},
            )


def _append_submission(task_id: str, n_trials: int = 2) -> TaskSweepSubmission:
    return TaskSweepSubmission(
        task_id=task_id,
        append_to_task=True,
        configs=[
            AgentModelPair(
                agent="claude-code",
                model="anthropic/claude-sonnet-4-6",
                n_trials=n_trials,
            )
        ],
        user="alice",
    )


async def _trial_count(maker, task_id: str) -> int:
    async with maker() as session:
        return await session.scalar(
            select(func.count())
            .select_from(TrialModel)
            .where(TrialModel.task_id == task_id)
        )


async def test_batch_one_bad_item_creates_valid_ones_and_reports_bad_index():
    """A batch with one bad item in the middle still creates the others.

    The items before and after the bad one still succeed, which only works if
    the savepoint rollback left the session usable for the items that follow.
    """
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _reset_schema_and_seed_tasks(engine, ["t-good-0", "t-good-2"])

        submissions = [
            _append_submission("t-good-0", n_trials=2),  # ok
            _append_submission("t-missing", n_trials=3),  # 404: task not seeded
            _append_submission("t-good-2", n_trials=1),  # ok, follows the bad one
        ]

        async with maker() as session:
            results = await create_task_sweep_batch_core(
                session, submissions=submissions, org_id=ORG_ID
            )
            await session.commit()

        assert [r.index for r in results] == [0, 1, 2]

        # Valid items succeeded with the trials they asked for.
        assert results[0].success is True
        assert results[0].status_code == 200
        assert results[0].task is not None
        assert results[0].task.trials_count == 2
        assert len(results[0].task.new_trial_ids) == 2

        assert results[2].success is True
        assert results[2].task is not None
        assert results[2].task.trials_count == 1

        # The bad item is reported by index, with its own status + error.
        assert results[1].success is False
        assert results[1].status_code == 404
        assert results[1].task is None
        assert "not found" in (results[1].error or "")

        # Persistence: succeeded items committed; the bad item wrote nothing.
        assert await _trial_count(maker, "t-good-0") == 2
        assert await _trial_count(maker, "t-good-2") == 1
        assert await _trial_count(maker, "t-missing") == 0
    finally:
        await engine.dispose()


async def test_batch_all_valid_items_succeed():
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _reset_schema_and_seed_tasks(engine, ["t-a", "t-b"])
        submissions = [
            _append_submission("t-a", n_trials=2),
            _append_submission("t-b", n_trials=3),
        ]
        async with maker() as session:
            results = await create_task_sweep_batch_core(
                session, submissions=submissions, org_id=ORG_ID
            )
            await session.commit()

        assert all(r.success for r in results)
        assert [r.status_code for r in results] == [200, 200]
        assert await _trial_count(maker, "t-a") == 2
        assert await _trial_count(maker, "t-b") == 3
    finally:
        await engine.dispose()


async def test_batch_validation_error_is_per_item_not_whole_batch():
    """An item that fails validation (empty configs) is a per-item 400.

    The sibling valid item must still be created -- validation is applied
    inside each item's savepoint, not as an all-or-nothing precheck.
    """
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _reset_schema_and_seed_tasks(engine, ["t-valid"])
        bad = TaskSweepSubmission(
            task_id="t-valid", append_to_task=True, configs=[], user="alice"
        )
        good = _append_submission("t-valid", n_trials=1)

        async with maker() as session:
            results = await create_task_sweep_batch_core(
                session, submissions=[bad, good], org_id=ORG_ID
            )
            await session.commit()

        assert results[0].success is False
        assert results[0].status_code == 400
        assert "configs" in (results[0].error or "")
        assert results[1].success is True
        # Only the good item's single trial landed.
        assert await _trial_count(maker, "t-valid") == 1
    finally:
        await engine.dispose()

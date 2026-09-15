"""Integration tests: run against a disposable database migrated to head."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text, update

from oddish.core.dashboard import load_dashboard_experiments
from oddish.core.experiment_summaries import refresh_experiment_summaries
from oddish.db import ExperimentModel, TaskModel, TrialModel, TrialStatus, get_session
from oddish.db.models import ExperimentSummaryModel, experiment_trials


async def page(session, org):
    return await load_dashboard_experiments(
        session,
        org_id=org,
        experiments_limit=25,
        experiments_offset=0,
        experiments_query=None,
        experiments_status="all",
    )


@pytest_asyncio.fixture
async def experiment():
    org = "prepared-" + uuid4().hex
    async with get_session() as session:
        exp = ExperimentModel(name=org, org_id=org)
        task = TaskModel(
            name=org, org_id=org, user="tester", task_path="s3://example/task"
        )
        session.add_all([exp, task])
        await session.flush()
        trial = TrialModel(
            id=org,
            name=org,
            task_id=task.id,
            experiment_id=exp.id,
            org_id=org,
            agent="codex",
            provider="openai",
            model="gpt-5.5",
            queue_key="test",
            status=TrialStatus.RUNNING,
        )
        session.add(trial)
        await session.commit()
        yield org, exp.id, trial.id
    async with get_session() as session:
        await session.execute(
            text("DELETE FROM trials WHERE org_id=:org"), {"org": org}
        )
        await session.execute(text("DELETE FROM tasks WHERE org_id=:org"), {"org": org})
        await session.execute(
            text("DELETE FROM experiments WHERE org_id=:org"), {"org": org}
        )
        await session.commit()


@pytest.mark.asyncio
async def test_prepared_read_and_durable_invalidation(experiment):
    org, eid, tid = experiment
    async with get_session() as session:
        rows, _ = await page(session, org)
        assert rows[0]["summary_pending"] is True
    assert await refresh_experiment_summaries() >= 1
    async with get_session() as session:
        rows, _ = await page(session, org)
        assert rows[0]["active_trials"] == 1
        assert rows[0]["summary_pending"] is False
        await session.execute(
            update(TrialModel)
            .where(TrialModel.id == tid)
            .values(status=TrialStatus.SUCCESS, reward=1)
        )
        await session.commit()
    async with get_session() as session:
        marker = await session.get(ExperimentSummaryModel, eid)
        assert marker.revision > marker.built_revision
        rows, _ = await page(session, org)
        assert rows[0]["active_trials"] == 1  # last complete result remains readable
    await refresh_experiment_summaries()
    async with get_session() as session:
        rows, _ = await page(session, org)
        assert rows[0]["completed_trials"] == 1
        assert rows[0]["active_trials"] == 0


@pytest.mark.asyncio
async def test_rollback_does_not_dirty_summary(experiment):
    _, eid, tid = experiment
    await refresh_experiment_summaries()
    async with get_session() as session:
        before = await session.scalar(
            select(ExperimentSummaryModel.revision).where(
                ExperimentSummaryModel.experiment_id == eid
            )
        )
        await session.execute(
            update(TrialModel)
            .where(TrialModel.id == tid)
            .values(status=TrialStatus.SUCCESS)
        )
        await session.rollback()
    async with get_session() as session:
        after = await session.scalar(
            select(ExperimentSummaryModel.revision).where(
                ExperimentSummaryModel.experiment_id == eid
            )
        )
        assert after == before


@pytest.mark.asyncio
async def test_change_during_build_remains_pending(experiment, monkeypatch):
    from oddish.core import dashboard

    org, eid, tid = experiment
    original = dashboard.rebuild_dashboard_experiments

    async def concurrent_change(session, **kwargs):
        rows = await original(session, **kwargs)
        async with get_session() as writer:
            await writer.execute(
                update(TrialModel)
                .where(TrialModel.id == tid)
                .values(status=TrialStatus.SUCCESS)
            )
            await writer.commit()
        return rows

    monkeypatch.setattr(dashboard, "rebuild_dashboard_experiments", concurrent_change)
    await asyncio.wait_for(refresh_experiment_summaries(), timeout=10)
    async with get_session() as session:
        marker = await session.get(ExperimentSummaryModel, eid)
        assert marker.revision > marker.built_revision
    monkeypatch.setattr(dashboard, "rebuild_dashboard_experiments", original)
    await refresh_experiment_summaries()
    async with get_session() as session:
        rows, _ = await page(session, org)
        assert rows[0]["completed_trials"] == 1


@pytest.mark.asyncio
async def test_collection_membership_dirties_both_experiments(experiment):
    org, eid, tid = experiment
    async with get_session() as session:
        collection = ExperimentModel(name="collection", org_id=org)
        session.add(collection)
        await session.flush()
        cid = collection.id
        await session.execute(
            experiment_trials.insert().values(experiment_id=cid, trial_id=tid)
        )
        await session.commit()
    await refresh_experiment_summaries()
    async with get_session() as session:
        await session.execute(
            update(TrialModel)
            .where(TrialModel.id == tid)
            .values(status=TrialStatus.SUCCESS)
        )
        await session.commit()
    async with get_session() as session:
        markers = (
            await session.scalars(
                select(ExperimentSummaryModel).where(
                    ExperimentSummaryModel.experiment_id.in_([eid, cid])
                )
            )
        ).all()
        assert len(markers) == 2
        assert all(m.revision > m.built_revision for m in markers)


@pytest.mark.asyncio
async def test_rebuild_failure_retries_without_discarding_summary(
    experiment, monkeypatch
):
    from oddish.core import dashboard
    from datetime import timedelta
    from oddish.db import utcnow

    org, eid, tid = experiment
    await refresh_experiment_summaries()
    async with get_session() as session:
        await session.execute(
            update(TrialModel)
            .where(TrialModel.id == tid)
            .values(status=TrialStatus.SUCCESS)
        )
        await session.commit()
    original = dashboard.rebuild_dashboard_experiments

    async def broken(*args, **kwargs):
        raise RuntimeError("simulated worker failure")

    monkeypatch.setattr(dashboard, "rebuild_dashboard_experiments", broken)
    assert await refresh_experiment_summaries() == 0
    async with get_session() as session:
        marker = await session.get(ExperimentSummaryModel, eid)
        assert marker.payload["active_trials"] == 1
        assert marker.revision > marker.built_revision
        assert marker.next_attempt_at > utcnow()
        marker.next_attempt_at = utcnow() - timedelta(seconds=1)
        await session.commit()
    monkeypatch.setattr(dashboard, "rebuild_dashboard_experiments", original)
    await refresh_experiment_summaries()
    async with get_session() as session:
        rows, _ = await page(session, org)
        assert rows[0]["completed_trials"] == 1


@pytest.mark.asyncio
async def test_reconciliation_repairs_clean_but_incorrect_summary(experiment):
    from datetime import timedelta
    from oddish.db import utcnow

    org, eid, _ = experiment
    await refresh_experiment_summaries()
    async with get_session() as session:
        marker = await session.get(ExperimentSummaryModel, eid)
        marker.payload = {**marker.payload, "active_trials": 999}
        marker.refreshed_at = utcnow() - timedelta(days=2)
        await session.commit()
    await refresh_experiment_summaries()
    async with get_session() as session:
        rows, _ = await page(session, org)
        assert rows[0]["active_trials"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("history_size", [1, 10000])
async def test_page_has_constant_statement_budget_as_trial_history_grows(
    experiment, history_size
):
    import time
    import statistics
    from sqlalchemy import event, insert
    import oddish.db.connection as connection
    from oddish.core.dashboard import (
        dashboard_experiment_rows,
        rebuild_dashboard_experiments,
    )

    org, eid, tid = experiment
    async with get_session() as session:
        task_id = await session.scalar(
            select(TrialModel.task_id).where(TrialModel.id == tid)
        )
        await session.execute(text("ANALYZE trials"))
        for offset in range(1, history_size, 500):
            await session.execute(
                insert(TrialModel),
                [
                    dict(
                        id=f"{tid}-{i}",
                        name=f"{tid}-{i}",
                        task_id=task_id,
                        experiment_id=eid,
                        org_id=org,
                        agent="codex",
                        provider="openai",
                        model="gpt-5.5",
                        queue_key="test",
                        status=TrialStatus.SUCCESS,
                    )
                    for i in range(offset, min(offset + 500, history_size))
                ],
            )
        await session.commit()
    async with get_session() as session:
        await session.execute(text("ANALYZE trials"))
    await refresh_experiment_summaries()
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(connection.engine.sync_engine, "before_cursor_execute", record)
    timings = []
    try:
        from oddish.db import get_read_session

        async with get_read_session() as session:
            for _ in range(10):
                started = time.perf_counter()
                rows, _ = await page(session, org)
                timings.append((time.perf_counter() - started) * 1000)
                assert rows[0]["total_trials"] == history_size
    finally:
        event.remove(connection.engine.sync_engine, "before_cursor_execute", record)
    selects = [
        sql.lower() for sql in statements if sql.lstrip().lower().startswith("select")
    ]
    assert len(selects) == 20  # one prepared page + one bounded tag lookup per read
    assert all("from trials" not in sql and "join trials" not in sql for sql in selects)
    async with get_session() as session:
        raw_rows = (
            (
                await session.execute(
                    dashboard_experiment_rows().where(ExperimentModel.id == eid)
                )
            )
            .mappings()
            .all()
        )
        started = time.perf_counter()
        await rebuild_dashboard_experiments(session, page_rows=raw_rows, org_id=org)
        rebuild_ms = (time.perf_counter() - started) * 1000
    print(
        f"BENCHMARK trials={history_size} prepared_median_ms={statistics.median(timings):.2f} aggregate_rebuild_ms={rebuild_ms:.2f} page_statements=2"
    )


@pytest.mark.asyncio
async def test_many_mutations_in_one_transaction_coalesce_to_one_revision(experiment):
    _, eid, tid = experiment
    async with get_session() as session:
        before = await session.scalar(
            select(ExperimentSummaryModel.revision).where(
                ExperimentSummaryModel.experiment_id == eid
            )
        )
        for _ in range(10):
            await session.execute(
                update(TrialModel)
                .where(TrialModel.id == tid)
                .values(status=TrialStatus.SUCCESS)
            )
        after = await session.scalar(
            select(ExperimentSummaryModel.revision).where(
                ExperimentSummaryModel.experiment_id == eid
            )
        )
        assert after == before + 1
        await session.commit()


@pytest.mark.asyncio
async def test_independent_health_sample_sees_pending_work(experiment):
    from oddish.core.experiment_summaries import prepared_read_health

    health = await prepared_read_health()
    assert health["summary_pending"] >= 1
    assert health["summary_lag_seconds"] >= 0
    await refresh_experiment_summaries()
    assert (await prepared_read_health())["summary_pending"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("summary_state", ["missing", "pending", "ready"])
async def test_mine_includes_ownerless_experiment_before_summary_is_ready(
    experiment, summary_state
):
    from oddish.db.models import task_experiments
    from sqlalchemy import delete

    org, eid, tid = experiment
    async with get_session() as session:
        trial = await session.get(TrialModel, tid)
        task = await session.get(TaskModel, trial.task_id)
        task.user = "owner-handle"
        await session.execute(
            task_experiments.insert().values(task_id=task.id, experiment_id=eid)
        )
        await session.commit()
    if summary_state == "ready":
        await refresh_experiment_summaries()
    elif summary_state == "missing":
        async with get_session() as session:
            await session.execute(
                delete(ExperimentSummaryModel).where(
                    ExperimentSummaryModel.experiment_id == eid
                )
            )
            await session.commit()
    async with get_session() as session:
        for handle, expected in [("owner-handle", [eid]), ("someone-else", [])]:
            rows, _ = await load_dashboard_experiments(
                session,
                org_id=org,
                experiments_limit=25,
                experiments_offset=0,
                experiments_query=None,
                experiments_status="all",
                experiments_author_user_id="member-id",
                experiments_author_github_usernames=[handle],
            )
            assert [row["id"] for row in rows] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("rebuild_fails", [False, True])
async def test_publication_and_retry_skip_writer_locks(
    experiment, monkeypatch, rebuild_fails
):
    from oddish.core import dashboard
    from oddish.db import task_experiments, utcnow

    org, eid, tid = experiment
    async with get_session() as session:
        task_id = await session.scalar(
            select(TrialModel.task_id).where(TrialModel.id == tid)
        )
        other = ExperimentModel(name="shared-task", org_id=org)
        session.add(other)
        await session.flush()
        other_id = other.id
        await session.execute(
            task_experiments.insert().values(task_id=task_id, experiment_id=other_id)
        )
    original = dashboard.rebuild_dashboard_experiments
    if rebuild_fails:

        async def broken(*args, **kwargs):
            raise RuntimeError("simulated rebuild failure")

        monkeypatch.setattr(dashboard, "rebuild_dashboard_experiments", broken)

    async with get_session() as writer:
        locked = await writer.scalar(
            select(ExperimentSummaryModel)
            .where(ExperimentSummaryModel.experiment_id == eid)
            .with_for_update()
        )
        before = (locked.built_revision, locked.next_attempt_at)
        # Maintenance must finish while the writer owns one of two summaries.
        # Without SKIP LOCKED this waits for the writer, which is waiting here.
        completed = await asyncio.wait_for(refresh_experiment_summaries(), timeout=5)
        assert completed == (0 if rebuild_fails else 1)
        await writer.refresh(locked)
        assert (locked.built_revision, locked.next_attempt_at) == before
        async with get_session() as reader:
            available = await reader.get(ExperimentSummaryModel, other_id)
            if rebuild_fails:
                assert available.next_attempt_at > utcnow()
            else:
                assert available.built_revision == available.revision
        # This task write dirties both experiments in the same transaction.
        await writer.execute(
            update(TaskModel).where(TaskModel.id == task_id).values(run_analysis=True)
        )
        await asyncio.wait_for(writer.commit(), timeout=5)
    monkeypatch.setattr(dashboard, "rebuild_dashboard_experiments", original)
    async with get_session() as session:
        await session.execute(
            update(ExperimentSummaryModel)
            .where(ExperimentSummaryModel.experiment_id.in_([eid, other_id]))
            .values(next_attempt_at=utcnow())
        )
    assert await asyncio.wait_for(refresh_experiment_summaries(), timeout=5) == 2
    async with get_session() as session:
        for experiment_id in [eid, other_id]:
            marker = await session.get(ExperimentSummaryModel, experiment_id)
            assert marker.revision == marker.built_revision
            assert marker.payload is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("upgrade_existing_trigger", [False, True])
async def test_status_only_completion_invalidates_pending_qa(
    experiment, upgrade_existing_trigger
):
    from oddish.db import TaskStatus, VerdictStatus, task_experiments

    org, eid, tid = experiment
    if upgrade_existing_trigger:
        import importlib.util
        from pathlib import Path
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location(
            "prepared_status_migration",
            Path(__file__).parents[1] / "alembic/versions/prepared_status_001.py",
        )
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        async with get_session() as session:
            connection = await session.connection()

            def upgrade_from_old_trigger(connection):
                migration.op = Operations(MigrationContext.configure(connection))
                migration.downgrade()
                definition = connection.scalar(
                    text(
                        "SELECT pg_get_triggerdef(oid) FROM pg_trigger "
                        "WHERE tgname='experiment_summary_changed' AND tgrelid='tasks'::regclass"
                    )
                )
                assert ", status" not in definition
                migration.upgrade()

            await connection.run_sync(upgrade_from_old_trigger)
    async with get_session() as session:
        task_id = await session.scalar(
            select(TrialModel.task_id).where(TrialModel.id == tid)
        )
        await session.execute(
            task_experiments.insert().values(task_id=task_id, experiment_id=eid)
        )
        await session.execute(
            update(TaskModel)
            .where(TaskModel.id == task_id)
            .values(
                run_analysis=True,
                status=TaskStatus.VERDICT_PENDING,
                verdict_status=VerdictStatus.SUCCESS,
            )
        )
    await refresh_experiment_summaries()
    async with get_session() as session:
        marker = await session.get(ExperimentSummaryModel, eid)
        before = marker.revision
        assert marker.payload["verdict_pending"] == 1
        await session.execute(
            update(TaskModel)
            .where(TaskModel.id == task_id)
            .values(status=TaskStatus.COMPLETED)
        )
    async with get_session() as session:
        marker = await session.get(ExperimentSummaryModel, eid)
        assert marker.revision == before + 1
        assert marker.built_revision == before
    await refresh_experiment_summaries()
    async with get_session() as session:
        rows, _ = await page(session, org)
        assert rows[0]["verdict_pending"] == 0


@pytest.mark.asyncio
async def test_pending_rebuild_precedes_daily_reconciliation(experiment):
    from datetime import timedelta
    from oddish.db import utcnow

    org, eid, tid = experiment
    async with get_session() as session:
        clean = ExperimentModel(name="daily-reconcile", org_id=org)
        session.add(clean)
        await session.flush()
        clean_id = clean.id
        await session.commit()
    await refresh_experiment_summaries()
    async with get_session() as session:
        marker = await session.get(ExperimentSummaryModel, clean_id)
        marker.refreshed_at = utcnow() - timedelta(days=2)
        marker.next_attempt_at = marker.refreshed_at
        await session.execute(
            update(TrialModel)
            .where(TrialModel.id == tid)
            .values(status=TrialStatus.SUCCESS)
        )
        await session.commit()
    assert await refresh_experiment_summaries(batch_size=1) == 1
    async with get_session() as session:
        pending = await session.get(ExperimentSummaryModel, eid)
        clean = await session.get(ExperimentSummaryModel, clean_id)
        assert pending.revision == pending.built_revision
        assert pending.payload["active_trials"] == 0
        assert clean.refreshed_at < utcnow() - timedelta(days=1)
    assert await refresh_experiment_summaries(batch_size=1) == 1
    async with get_session() as session:
        clean = await session.get(ExperimentSummaryModel, clean_id)
        assert clean.refreshed_at > utcnow() - timedelta(minutes=1)

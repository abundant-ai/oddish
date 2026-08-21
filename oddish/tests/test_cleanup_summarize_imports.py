"""Recovery tests for summarize trials that settle before publication."""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from oddish.db import TaskModel, TaskStatus, TrialModel, TrialStatus, get_session
from oddish.db.models import ExperimentModel
from oddish.workers.queue.cleanup import (
    STALE_SUMMARIZE_IMPORT_BATCH_LIMIT,
    _heal_stale_summarize_imports,
)

URL = os.environ.get("ODDISH_DATABASE_URL")


@pytest.mark.asyncio
async def test_summarize_reimport_scan_uses_the_bounded_batch():
    result = SimpleNamespace(all=lambda: [("summary-1",)])
    session = SimpleNamespace(execute=AsyncMock(return_value=result))

    assert await _heal_stale_summarize_imports(session) == ["summary-1"]
    statement, params = session.execute.await_args.args
    sql = str(statement)
    assert "target.trajectory_summary_refresh_trial_id" in sql
    assert "refresh.status::text = 'SUCCESS'" in sql
    assert params == {"batch_limit": STALE_SUMMARIZE_IMPORT_BATCH_LIMIT}
    assert STALE_SUMMARIZE_IMPORT_BATCH_LIMIT == 200


@pytest.mark.asyncio
async def test_cleanup_candidate_reimports_summary_and_clears_pointer(monkeypatch):
    """Needs PostgreSQL. This models death after SUCCESS and before import."""
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    from oddish.db import init_db
    from oddish.workers import analysis_trials
    from oddish.workers.analysis_trials import (
        get_or_create_summarize_trial,
        handle_analysis_trial_settled,
    )

    await init_db()
    suffix = uuid.uuid4().hex[:8]
    task_id = f"summarize-cleanup-{suffix}"
    target_id = f"{task_id}-target"
    async with get_session() as session:
        experiment = ExperimentModel(name=f"summarize-cleanup-{suffix}")
        task = TaskModel(
            id=task_id,
            name=task_id,
            user="u",
            task_path="p",
            status=TaskStatus.COMPLETED,
        )
        session.add_all([experiment, task])
        await session.flush()
        await session.execute(
            text(
                "INSERT INTO task_experiments (task_id, experiment_id, created_at) "
                "VALUES (:task_id, :experiment_id, NOW())"
            ),
            {"task_id": task_id, "experiment_id": experiment.id},
        )
        session.add(
            TrialModel(
                id=target_id,
                name=target_id,
                task_id=task_id,
                experiment_id=experiment.id,
                agent="claude-code",
                provider="local",
                queue_key="q",
                kind="agent",
                status=TrialStatus.SUCCESS,
                has_trajectory=True,
                attempts=1,
                max_attempts=3,
            )
        )

    async with get_session() as session:
        summarize = await get_or_create_summarize_trial(
            session, target_trial_id=target_id
        )
        assert summarize is not None
        summarize_id = summarize.id
    async with get_session() as session:
        summarize = await session.get(TrialModel, summarize_id)
        summarize.status = TrialStatus.SUCCESS

    async with get_session() as session:
        candidates = await _heal_stale_summarize_imports(session)
    assert summarize_id in candidates

    async def read_artifact(_trial, _filename):
        return {
            "target_trial_id": target_id,
            "trajectory_summary": {
                "summary": "Recovered after interrupted publication.",
                "highlights": [],
                "components": [
                    {
                        "step_ids": [1],
                        "trajectory_component": "implementing",
                        "action": "edit",
                        "purpose": "build",
                        "summary": "One edit.",
                    }
                ],
            },
        }

    async def no_trajectory(_trial):
        return None

    monkeypatch.setattr(analysis_trials, "read_analysis_artifact", read_artifact)
    monkeypatch.setattr("oddish.core.trial_io.read_trial_trajectory", no_trajectory)
    await handle_analysis_trial_settled(summarize_id)

    async with get_session() as session:
        target = await session.get(TrialModel, target_id)
        assert target.trajectory_summary["summary"] == (
            "Recovered after interrupted publication."
        )
        assert target.trajectory_summary_refresh_trial_id is None

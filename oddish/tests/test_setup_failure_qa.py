"""QA admission and delivery drop provider setup failures that never ran."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from oddish.core.delivery_qa import counts_as_delivery_qa_source
from oddish.db import TaskStatus, VerdictStatus


def _trial(**overrides):
    row = SimpleNamespace(
        result={"harbor_exception": {"exception_type": "NotFoundError"}},
        error_message="NotFoundError: The model does not exist",
        input_tokens=None,
        output_tokens=None,
        has_trajectory=False,
        total_steps=None,
        agent="claude-code",
    )
    row.__dict__.update(overrides)
    return row


def test_delivery_drops_setup_failure_without_work():
    assert counts_as_delivery_qa_source(_trial(), sql_eligible=True) is False


def test_delivery_keeps_sql_eligible_real_run():
    assert (
        counts_as_delivery_qa_source(_trial(input_tokens=8), sql_eligible=True) is True
    )


def test_delivery_keeps_nop_baseline():
    assert (
        counts_as_delivery_qa_source(
            _trial(
                agent="nop",
                result={},
                error_message=None,
            ),
            sql_eligible=False,
        )
        is True
    )


class _FakeSession:
    async def get(self, *args, **kwargs):
        return None

    async def scalars(self, *args, **kwargs):
        return []


@pytest.mark.asyncio
async def test_start_qa_zero_eligible_clears_published_verdict(monkeypatch):
    from oddish.queue import start_qa_for_task

    task = SimpleNamespace(
        id="task-1",
        current_version_id=None,
        status=TaskStatus.RUNNING,
        finished_at=None,
        verdict={"verdict": "accept", "is_good": True},
        verdict_status=VerdictStatus.SUCCESS,
        verdict_error=None,
        verdict_started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        verdict_finished_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        "oddish.queue.qa_eligible_trial_ids", AsyncMock(return_value=[])
    )

    queued = await start_qa_for_task(_FakeSession(), task)

    assert queued is False
    assert task.status == TaskStatus.COMPLETED
    assert task.verdict is None
    assert task.verdict_status == VerdictStatus.FAILED
    assert "Insufficient evidence" in (task.verdict_error or "")


@pytest.mark.asyncio
async def test_backfill_withdraws_before_zero_eligible_admission(monkeypatch):
    from oddish.core.endpoints import qa as qa_mod
    from oddish.core.verdict_state import fail_verdict
    from oddish.db import utcnow

    task = SimpleNamespace(
        id="task-1",
        org_id=None,
        current_version_id=None,
        status=TaskStatus.COMPLETED,
        finished_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        verdict={"verdict": "accept", "is_good": True},
        verdict_status=VerdictStatus.SUCCESS,
        verdict_error=None,
        verdict_started_at=None,
        verdict_finished_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        trials=[
            SimpleNamespace(
                id="t1",
                kind="agent",
                superseded_by_trial_id=None,
                task_version_id=None,
            )
        ],
    )

    class _Result:
        def scalar_one_or_none(self):
            return task

    class _Session:
        async def execute(self, *args, **kwargs):
            return _Result()

        async def commit(self):
            return None

    async def fake_start(session, t, *, environment=None):
        assert t.verdict is None
        assert t.verdict_status == VerdictStatus.QUEUED
        t.status = TaskStatus.COMPLETED
        fail_verdict(
            t,
            error="Insufficient evidence: no eligible solver trials for the current task version.",
            now=utcnow(),
        )
        return False

    monkeypatch.setattr(
        "oddish.queue.live_analysis_trial_id", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "oddish.queue.task_audit_pending", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        "oddish.queue.qa_eligible_trial_ids", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("oddish.queue.start_qa_for_task", fake_start)
    monkeypatch.setattr(qa_mod, "_count_active_trials", AsyncMock(return_value=0))

    result = await qa_mod.backfill_task_analysis_core(_Session(), task_id="task-1")

    assert result["status"] == "completed"
    assert task.verdict is None
    assert task.verdict_status == VerdictStatus.FAILED

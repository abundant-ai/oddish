"""Regression tests for retry scheduling in cleanup safety nets."""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.db import TrialModel, TrialStatus  # noqa: E402
from oddish.workers.queue import cleanup  # noqa: E402


class _FakeResult:
    def __init__(
        self,
        *,
        rows: list[Any] | None = None,
        mappings_rows: list[dict[str, Any]] | None = None,
        rowcount: int = 0,
    ) -> None:
        self._rows = rows or []
        self._mappings_rows = mappings_rows or []
        self.rowcount = rowcount

    def all(self) -> list[Any]:
        if self._mappings_rows:
            return self._mappings_rows
        return self._rows

    def mappings(self) -> "_FakeResult":
        return self


class _FakeSession:
    def __init__(self, trial: SimpleNamespace) -> None:
        self.trial = trial
        self.worker_job_retry_updates: list[dict[str, Any]] = []

    async def execute(self, statement, params: dict[str, Any] | None = None):
        sql = str(statement)
        if "UPDATE worker_jobs" in sql and "SET    status = CASE" in sql:
            return _FakeResult(
                mappings_rows=[
                    {
                        "id": "wj-1",
                        "kind": "TRIAL",
                        "new_status": "RETRYING",
                        "subject_table": "trials",
                        "subject_id": self.trial.id,
                        "attempts": 2,
                        "max_attempts": 6,
                        "error_message": "Worker heartbeat stalled for over 15 minutes.",
                    }
                ]
            )
        if "UPDATE worker_jobs" in sql and "available_after = :retry_at" in sql:
            self.worker_job_retry_updates.append(params or {})
            return _FakeResult(rowcount=1)
        return _FakeResult()

    async def get(self, model, object_id: str):
        if model is TrialModel and object_id == self.trial.id:
            return self.trial
        return None

    async def flush(self) -> None:
        return None


@pytest.mark.asyncio
async def test_stale_trial_retry_cleanup_schedules_backoff(monkeypatch):
    trial = SimpleNamespace(
        id="trial-1",
        status=TrialStatus.RUNNING,
        error_message=None,
        next_retry_at=None,
        current_worker_id="worker-1",
        current_queue_slot=3,
        stale_reaped_at=None,
    )
    session = _FakeSession(trial)

    @asynccontextmanager
    async def fake_get_session():
        yield session

    async def no_stage_transition(_session, _trial_id):
        return False

    async def fake_reap_idle_in_transaction_zombies():
        return 0

    monkeypatch.setattr(cleanup, "get_session", fake_get_session)
    monkeypatch.setattr(
        cleanup,
        "reap_idle_in_transaction_zombies",
        fake_reap_idle_in_transaction_zombies,
    )
    monkeypatch.setattr("oddish.queue.maybe_start_analysis_stage", no_stage_transition)
    monkeypatch.setattr("oddish.queue.maybe_start_verdict_stage", no_stage_transition)
    monkeypatch.setattr(
        cleanup,
        "calculate_trial_retry_delay_seconds",
        lambda *, attempts, error_message: 60.0,
    )

    before = datetime.now(timezone.utc)
    result = await cleanup.cleanup_orphaned_queue_state(stale_after_minutes=15)
    after = datetime.now(timezone.utc)

    assert result["worker_jobs_retried"] == 1
    assert len(session.worker_job_retry_updates) == 1

    retry_at = session.worker_job_retry_updates[0]["retry_at"]
    assert before + timedelta(seconds=60) <= retry_at <= after + timedelta(seconds=60)

    assert trial.status == TrialStatus.RETRYING
    assert trial.error_message == "Worker heartbeat stalled for over 15 minutes."
    assert trial.next_retry_at == retry_at
    assert trial.current_worker_id is None
    assert trial.current_queue_slot is None

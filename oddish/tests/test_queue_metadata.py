from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.helpers import (  # noqa: E402
    _QueueSnapshotTrial,
    _build_trial_queue_info_snapshot,
    fetch_trial_queue_info,
)
from oddish.db import Priority, TrialModel, TrialStatus, WorkerJobStatus  # noqa: E402


def _ts(offset_seconds: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=offset_seconds)


def test_queue_snapshot_matches_priority_and_fair_scheduling(monkeypatch):
    monkeypatch.setattr(
        "oddish.config.Settings.get_model_concurrency",
        lambda self, queue_key: 4,
    )

    snapshot = [
        _QueueSnapshotTrial(
            trial_id="running-a",
            queue_key="openai/gpt-5.4",
            status=TrialStatus.RUNNING,
            created_at=_ts(0),
            priority=Priority.LOW,
            fairness_key="user-a",
        ),
        _QueueSnapshotTrial(
            trial_id="high-a-1",
            queue_key="openai/gpt-5.4",
            status=TrialStatus.QUEUED,
            created_at=_ts(10),
            priority=Priority.HIGH,
            fairness_key="user-a",
        ),
        _QueueSnapshotTrial(
            trial_id="high-a-2",
            queue_key="openai/gpt-5.4",
            status=TrialStatus.QUEUED,
            created_at=_ts(30),
            priority=Priority.HIGH,
            fairness_key="user-a",
        ),
        _QueueSnapshotTrial(
            trial_id="high-b-1",
            queue_key="openai/gpt-5.4",
            status=TrialStatus.QUEUED,
            created_at=_ts(20),
            priority=Priority.HIGH,
            fairness_key="user-b",
        ),
        _QueueSnapshotTrial(
            trial_id="low-b-1",
            queue_key="openai/gpt-5.4",
            status=TrialStatus.QUEUED,
            created_at=_ts(5),
            priority=Priority.LOW,
            fairness_key="user-b",
        ),
        _QueueSnapshotTrial(
            trial_id="low-c-1",
            queue_key="openai/gpt-5.4",
            status=TrialStatus.QUEUED,
            created_at=_ts(1),
            priority=Priority.LOW,
            fairness_key="user-c",
        ),
    ]

    queue_info = _build_trial_queue_info_snapshot(
        snapshot,
        target_trial_ids={
            "high-a-1",
            "high-a-2",
            "high-b-1",
            "low-b-1",
            "low-c-1",
        },
    )

    assert queue_info["high-b-1"].position == 1
    assert queue_info["high-a-1"].position == 2
    assert queue_info["high-a-2"].position == 3
    assert queue_info["low-c-1"].position == 4
    assert queue_info["low-b-1"].position == 5

    assert queue_info["low-b-1"].ahead == 4
    assert queue_info["low-b-1"].queued_count == 5
    assert queue_info["low-b-1"].running_count == 1
    assert queue_info["low-b-1"].concurrency_limit == 4


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _CapturingSession:
    def __init__(self, rows):
        self.rows = rows
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _FakeResult(self.rows)


@pytest.mark.asyncio
async def test_fetch_trial_queue_info_uses_runnable_worker_jobs(monkeypatch):
    monkeypatch.setattr(
        "oddish.config.Settings.get_model_concurrency",
        lambda self, queue_key: 4,
    )

    backoff_trial = TrialModel(
        id="backoff",
        status=TrialStatus.RETRYING,
        queue_key="openai/gpt-5.4",
    )
    runnable_trial = TrialModel(
        id="runnable",
        status=TrialStatus.QUEUED,
        queue_key="openai/gpt-5.4",
    )
    session = _CapturingSession(
        [
            SimpleNamespace(
                id="runnable",
                queue_key="openai/gpt-5.4",
                worker_job_status=WorkerJobStatus.QUEUED,
                created_at=_ts(10),
                priority=Priority.LOW,
                fairness_key="user-a",
            )
        ]
    )

    queue_info = await fetch_trial_queue_info(
        session, trials=[backoff_trial, runnable_trial]
    )

    assert "backoff" not in queue_info
    assert queue_info["runnable"].position == 1
    assert queue_info["runnable"].queued_count == 1

    sql = str(session.statement.compile(compile_kwargs={"literal_binds": False}))
    assert "worker_jobs.available_after <= now()" in sql
    assert "worker_jobs.status IN" in sql

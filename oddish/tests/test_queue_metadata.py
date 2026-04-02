from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.api.helpers import (  # noqa: E402
    _QueueSnapshotTrial,
    _build_trial_queue_info_snapshot,
)
from oddish.db import Priority, TrialStatus  # noqa: E402


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

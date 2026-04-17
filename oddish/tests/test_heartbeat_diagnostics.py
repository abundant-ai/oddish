"""Tests for the heartbeat resilience and diagnostics changes.

These cover the fix for the incident where a 17-minute Supabase pooler
blip caused the whole worker fleet's heartbeats to fail, and cleanup then
reaped 25 healthy trials in a single sweep -- with no DB evidence of
*why* the heartbeats stopped.

Specifically:
- `_heartbeat_trial_execution` retries silently on DB failures and flushes
  accumulated failure info (`heartbeat_failure_count`, `last_heartbeat_error`,
  `last_heartbeat_error_at`) on the next successful write.
- `cleanup_orphaned_queue_state` records `stale_reaped_at` without
  clobbering `heartbeat_at`, and includes heartbeat-failure breadcrumbs
  in the trial's error message when they are present.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.workers.queue import trial_handler  # noqa: E402


@pytest.mark.asyncio
async def test_heartbeat_accumulates_failures_and_flushes_on_recovery(monkeypatch):
    """Heartbeat failures are stashed locally and flushed on the next success.

    Simulates the exact pooler-blip scenario: the DB write raises a few
    times, then recovers. The first successful write should carry the
    accumulated failure count + the most recent error message forward.
    """

    touch_calls: list[dict[str, Any]] = []
    behaviors = iter(
        [
            ConnectionError("pooler unreachable #1"),
            ConnectionError("pooler unreachable #2"),
            None,
        ]
    )

    async def fake_touch(
        *,
        trial_id: str,
        worker_id: str | None,
        queue_slot: int | None,
        claimed: bool = False,
        pending_failure_count: int = 0,
        pending_last_error: str | None = None,
        pending_last_error_at: Any = None,
    ) -> None:
        touch_calls.append(
            {
                "trial_id": trial_id,
                "pending_failure_count": pending_failure_count,
                "pending_last_error": pending_last_error,
                "had_error_at": pending_last_error_at is not None,
            }
        )
        behavior = next(behaviors)
        if isinstance(behavior, Exception):
            raise behavior

    monkeypatch.setattr(trial_handler, "_touch_trial_execution", fake_touch)
    monkeypatch.setattr(trial_handler, "TRIAL_HEARTBEAT_INTERVAL_SECONDS", 0)

    stop_event = asyncio.Event()
    task = asyncio.create_task(
        trial_handler._heartbeat_trial_execution(
            trial_id="trial-1",
            worker_id="worker-1",
            queue_slot=3,
            stop_event=stop_event,
        )
    )

    for _ in range(200):
        if len(touch_calls) >= 3:
            break
        await asyncio.sleep(0)

    stop_event.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert len(touch_calls) >= 3

    first, second, third = touch_calls[:3]

    assert first["pending_failure_count"] == 0
    assert first["pending_last_error"] is None
    assert first["had_error_at"] is False

    assert second["pending_failure_count"] == 1
    assert second["pending_last_error"] is not None
    assert "pooler unreachable #1" in second["pending_last_error"]
    assert second["had_error_at"] is True

    assert third["pending_failure_count"] == 2
    assert "pooler unreachable #2" in (third["pending_last_error"] or "")
    assert third["had_error_at"] is True


@pytest.mark.asyncio
async def test_touch_trial_execution_flushes_pending_failure_metadata(monkeypatch):
    """_touch_trial_execution persists pending failure info onto the row."""

    from oddish.db import TrialStatus

    trial = SimpleNamespace(
        id="trial-1",
        status=TrialStatus.RUNNING,
        current_worker_id=None,
        current_queue_slot=None,
        claimed_at=None,
        heartbeat_at=None,
        heartbeat_failure_count=0,
        last_heartbeat_error=None,
        last_heartbeat_error_at=None,
    )

    class _FakeSession:
        async def get(self, _model, _trial_id):
            return trial

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_trial_session(_trial_id, *, allow_missing=False):
        yield _FakeSession(), trial

    monkeypatch.setattr(trial_handler, "_trial_session", fake_trial_session)

    from datetime import datetime, timezone

    error_ts = datetime(2026, 4, 17, 21, 0, 0, tzinfo=timezone.utc)

    await trial_handler._touch_trial_execution(
        trial_id="trial-1",
        worker_id="worker-1",
        queue_slot=7,
        pending_failure_count=3,
        pending_last_error="ConnectionError: pooler unreachable",
        pending_last_error_at=error_ts,
    )

    assert trial.current_worker_id == "worker-1"
    assert trial.current_queue_slot == 7
    assert trial.heartbeat_at is not None
    assert trial.heartbeat_failure_count == 3
    assert trial.last_heartbeat_error == "ConnectionError: pooler unreachable"
    assert trial.last_heartbeat_error_at == error_ts


@pytest.mark.asyncio
async def test_touch_trial_execution_truncates_long_errors(monkeypatch):
    """We aggressively cap last_heartbeat_error to keep the row small."""

    from contextlib import asynccontextmanager

    from oddish.db import TrialStatus

    trial = SimpleNamespace(
        id="trial-1",
        status=TrialStatus.RUNNING,
        current_worker_id=None,
        current_queue_slot=None,
        claimed_at=None,
        heartbeat_at=None,
        heartbeat_failure_count=0,
        last_heartbeat_error=None,
        last_heartbeat_error_at=None,
    )

    class _FakeSession:
        async def get(self, _model, _trial_id):
            return trial

    @asynccontextmanager
    async def fake_trial_session(_trial_id, *, allow_missing=False):
        yield _FakeSession(), trial

    monkeypatch.setattr(trial_handler, "_trial_session", fake_trial_session)

    huge_error = "x" * 5000
    await trial_handler._touch_trial_execution(
        trial_id="trial-1",
        worker_id="worker-1",
        queue_slot=0,
        pending_failure_count=1,
        pending_last_error=huge_error,
    )

    assert trial.last_heartbeat_error is not None
    assert len(trial.last_heartbeat_error) == trial_handler._HEARTBEAT_ERROR_MAX_LEN

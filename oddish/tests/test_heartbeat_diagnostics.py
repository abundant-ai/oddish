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

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.workers.queue import trial_handler  # noqa: E402


@pytest.mark.asyncio
async def test_heartbeat_also_writes_worker_jobs_when_job_id_provided(monkeypatch):
    """Regression guard: the trial heartbeat must touch worker_jobs too.

    The unified stale-reap sweep in ``cleanup.py`` reads
    ``worker_jobs.heartbeat_at``. If the heartbeat loop only writes to
    ``trials.heartbeat_at`` (the pre-unification behavior), long trials
    get falsely reaped after ``STALE_HEARTBEAT_MINUTES``. Harbor trials
    can run 12h -- so skipping the worker_jobs write is catastrophic.
    """

    async def fake_touch(**kwargs):
        # Pretend the trial row was touched successfully and owns the trial.
        return True

    wj_calls: list[dict] = []

    async def fake_heartbeat_worker_job(job_id, **kwargs):
        wj_calls.append({"job_id": job_id, **kwargs})

    monkeypatch.setattr(trial_handler, "_touch_trial_execution", fake_touch)
    monkeypatch.setattr(
        trial_handler, "heartbeat_worker_job", fake_heartbeat_worker_job
    )
    monkeypatch.setattr(trial_handler, "TRIAL_HEARTBEAT_INTERVAL_SECONDS", 0)

    stop_event = asyncio.Event()
    task = asyncio.create_task(
        trial_handler._heartbeat_trial_execution(
            trial_id="trial-1",
            worker_id="worker-1",
            queue_slot=3,
            stop_event=stop_event,
            worker_job_id="wj-1",
        )
    )

    for _ in range(200):
        if len(wj_calls) >= 1:
            break
        await asyncio.sleep(0)

    stop_event.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert len(wj_calls) >= 1
    assert wj_calls[0]["job_id"] == "wj-1"


@pytest.mark.asyncio
async def test_heartbeat_skips_worker_jobs_write_when_job_id_absent(monkeypatch):
    """Legacy callers (no worker_job_id) should still work without worker_jobs."""

    async def fake_touch(**kwargs):
        return None

    wj_calls: list[dict] = []

    async def fake_heartbeat_worker_job(job_id, **kwargs):
        wj_calls.append({"job_id": job_id})

    monkeypatch.setattr(trial_handler, "_touch_trial_execution", fake_touch)
    monkeypatch.setattr(
        trial_handler, "heartbeat_worker_job", fake_heartbeat_worker_job
    )
    monkeypatch.setattr(trial_handler, "TRIAL_HEARTBEAT_INTERVAL_SECONDS", 0)

    stop_event = asyncio.Event()
    task = asyncio.create_task(
        trial_handler._heartbeat_trial_execution(
            trial_id="trial-1",
            worker_id="worker-1",
            queue_slot=3,
            stop_event=stop_event,
            # No worker_job_id -- the worker_jobs write is skipped.
        )
    )

    # Let the loop tick a couple of times to be sure.
    for _ in range(50):
        await asyncio.sleep(0)

    stop_event.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert wj_calls == []


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
        superseded_by_trial_id=None,
        deleted_at=None,
    )

    class _FakeSession:
        async def get(self, _model, _trial_id):
            return trial

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_trial_session(
        _trial_id, *, allow_missing=False, with_for_update=False
    ):
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
        superseded_by_trial_id=None,
        deleted_at=None,
    )

    class _FakeSession:
        async def get(self, _model, _trial_id):
            return trial

    @asynccontextmanager
    async def fake_trial_session(
        _trial_id, *, allow_missing=False, with_for_update=False
    ):
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


@pytest.mark.asyncio
async def test_heartbeat_covers_post_execution_upload_and_store(
    monkeypatch, tmp_path
):
    """Regression guard: the heartbeat must outlive Harbor execution.

    The S3 upload, sauron mirror, and result store run after Harbor returns
    but before the trial turns terminal. When the heartbeat stopped at the
    end of ``_execute_trial``, that tail had a hard 15-minute budget before
    the stale-reap sweep killed the job -- and ``_store_trial_results`` then
    discarded the finished result on its ownership check. So completed
    (sometimes passing) trials were reaped as "Worker heartbeat stalled"
    and re-run from scratch.
    """

    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from oddish.db import TrialStatus
    from oddish.workers.harbor.outcome import HarborOutcome

    events: list[str] = []
    heartbeat_kwargs: dict[str, Any] = {}

    async def fake_heartbeat(*, stop_event, **kwargs):
        heartbeat_kwargs.update(kwargs)
        events.append("heartbeat_start")
        await stop_event.wait()
        events.append("heartbeat_stop")

    trial = SimpleNamespace(
        id="trial-1",
        status=TrialStatus.RUNNING,
        agent="claude-code",
        idempotency_key=None,
    )

    @asynccontextmanager
    async def fake_trial_session(
        _trial_id, *, allow_missing=False, with_for_update=False
    ):
        yield None, trial

    @asynccontextmanager
    async def fake_get_session():
        yield SimpleNamespace(
            execute=_async_return(SimpleNamespace(rowcount=1)),
        )

    def _async_return(value):
        async def _inner(*args, **kwargs):
            return value

        return _inner

    job_dir = tmp_path / "job"
    job_dir.mkdir()

    async def fake_execute_trial(**kwargs):
        # Yield once so the heartbeat task (created just before execution)
        # gets scheduled, like real execution's constant awaits allow.
        await asyncio.sleep(0)
        events.append("execute")
        return trial_handler.TrialExecutionResult(
            outcome=HarborOutcome(
                reward=1.0,
                error=None,
                exit_code=0,
                duration_sec=1.0,
                job_result_path=None,
                job_dir=job_dir,
            ),
            execution_error=None,
            tailed_attempt=1,
        )

    async def fake_upload(trial_id, _job_dir, authorized_prefix=None):
        events.append("upload")
        return "trials/trial-1"

    async def fake_store(**kwargs):
        events.append("store")
        return True, False

    async def fake_settlement(**kwargs):
        events.append("settle")

    async def fake_noop(*args, **kwargs):
        return None

    prepared = trial_handler.PreparedTrialRun(
        task_path=None,
        task_s3_key="tasks/task-1",
        task_id="task-1",
        trial_agent="claude-code",
        trial_model="test/model",
        trial_environment="daytona",
        trial_harbor_config=None,
    )

    monkeypatch.setattr(
        trial_handler, "_heartbeat_trial_execution", fake_heartbeat
    )
    monkeypatch.setattr(trial_handler, "_trial_session", fake_trial_session)
    monkeypatch.setattr(trial_handler, "get_session", fake_get_session)
    monkeypatch.setattr(
        trial_handler, "_prepare_trial_run", _async_return(prepared)
    )
    monkeypatch.setattr(
        trial_handler.byok, "byok_resolver_registered", lambda: False
    )
    monkeypatch.setattr(
        trial_handler, "trial_llm_key_hash", lambda *a, **k: None
    )
    monkeypatch.setattr(
        trial_handler,
        "resolve_task_directory",
        _async_return((tmp_path, None, "tasks/task-1")),
    )
    monkeypatch.setattr(
        trial_handler, "capture_sandbox_resources", lambda *a, **k: None
    )
    monkeypatch.setattr(
        trial_handler, "capture_verifier_resources", lambda *a, **k: None
    )
    monkeypatch.setattr(
        trial_handler.settings, "harbor_jobs_dir", str(tmp_path / "jobs")
    )
    monkeypatch.setattr(
        trial_handler.settings, "job_scoped_tokens_enabled", False
    )
    monkeypatch.setattr(trial_handler, "_execute_trial", fake_execute_trial)
    monkeypatch.setattr(trial_handler, "_settle_compute_costs", fake_noop)
    monkeypatch.setattr(
        trial_handler,
        "get_storage_client",
        lambda: SimpleNamespace(upload_trial_results=fake_upload),
    )
    import oddish.integrations.sauron as sauron_module

    monkeypatch.setattr(
        sauron_module,
        "get_sauron_uploader",
        lambda: SimpleNamespace(is_enabled=lambda: False),
    )
    monkeypatch.setattr(
        trial_handler, "_cleanup_uploaded_job_dir", lambda *a, **k: None
    )
    monkeypatch.setattr(trial_handler, "_store_trial_results", fake_store)
    monkeypatch.setattr(
        trial_handler, "_finish_trial_settlement", fake_settlement
    )
    monkeypatch.setattr(trial_handler.live_tail, "purge_events", fake_noop)
    monkeypatch.setattr(
        trial_handler, "_cleanup_trial_wrapper_dirs", lambda *a, **k: None
    )

    await trial_handler.run_trial_job(
        "trial-1",
        "queue-key",
        worker_id="worker-1",
        queue_slot=3,
        worker_job_id="wj-1",
        worker_job_attempt=1,
    )

    assert events.index("heartbeat_start") < events.index("execute")
    # The whole point: the heartbeat stops only after the result is stored
    # and settled, not when Harbor execution returns.
    assert events.index("heartbeat_stop") > events.index("upload")
    assert events.index("heartbeat_stop") > events.index("store")
    assert events.index("heartbeat_stop") > events.index("settle")
    # The stale-reap sweep reads worker_jobs.heartbeat_at, so the loop must
    # keep receiving the worker_job_id.
    assert heartbeat_kwargs["worker_job_id"] == "wj-1"
    assert heartbeat_kwargs["queue_slot"] == 3

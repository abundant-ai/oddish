"""Phase B tests for the unified `worker_jobs` enqueue/claim/dispatch path.

These exercise the dispatcher scaffolding without a live database:

- ``enqueue_worker_job`` builds a row with the right fields and
  delegates payload validation to the registered handler.
- ``run_single_worker_job`` routes the claimed row to its handler,
  records SUCCESS / RETRYING / FAILED correctly, and fails gracefully
  when no handler is registered.
- The unified claim SQL carries the invariants the rest of the design
  depends on (``FOR UPDATE SKIP LOCKED``, ``priority DESC``,
  status-filter, ``available_after`` gate, ``attempts`` increment).
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.db import (  # noqa: E402
    WorkerJobKind,
    WorkerJobModel,
    WorkerJobStatus,
)
from oddish.workers.jobs import (  # noqa: E402
    EnqueueRequest,
    JobOutcome,
    clear_handlers,
    enqueue_worker_job,
    register,
)
from oddish.workers.queue import worker_job_single_job  # noqa: E402
from oddish.workers.queue.worker_job_single_job import (  # noqa: E402
    ClaimedWorkerJob,
    _CLAIM_WORKER_JOB_SQL,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeSession:
    """Minimum AsyncSession surface exercised by ``enqueue_worker_job``."""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.flushed: bool = False

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed = True


class _FakeHandler:
    def __init__(
        self,
        kind: WorkerJobKind,
        *,
        outcome: JobOutcome | None = None,
        raise_exc: Exception | None = None,
        payload_validator=None,
    ) -> None:
        self.kind = kind
        self._outcome = outcome or JobOutcome.ok()
        self._raise = raise_exc
        self._validator = payload_validator
        self.run_calls: list[Any] = []

    def default_queue_key(self, job):  # type: ignore[override]
        return "default"

    def validate_payload(self, payload):  # type: ignore[override]
        if self._validator is not None:
            return self._validator(payload)
        return payload

    async def run(self, job):  # type: ignore[override]
        self.run_calls.append(job)
        if self._raise is not None:
            raise self._raise
        return self._outcome


@pytest.fixture(autouse=True)
def _reset_registry():
    clear_handlers()
    yield
    clear_handlers()


# ---------------------------------------------------------------------------
# enqueue_worker_job
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_builds_row_with_expected_fields():
    session = _FakeSession()
    request = EnqueueRequest(
        kind=WorkerJobKind.ANALYSIS,
        queue_key="analysis",
        payload={"trial_id": "t-1"},
        subject_table="trials",
        subject_id="t-1",
        priority=3,
        max_attempts=4,
        org_id="org-abc",
    )

    row = await enqueue_worker_job(session, request)

    assert session.flushed is True
    assert session.added == [row]
    assert isinstance(row, WorkerJobModel)
    assert row.kind == WorkerJobKind.ANALYSIS
    assert row.status == WorkerJobStatus.QUEUED
    assert row.queue_key == "analysis"
    assert row.subject_table == "trials"
    assert row.subject_id == "t-1"
    assert row.priority == 3
    assert row.max_attempts == 4
    assert row.attempts == 0
    assert row.payload == {"trial_id": "t-1"}
    assert row.org_id == "org-abc"
    assert row.available_after is not None
    assert row.id  # auto-generated


@pytest.mark.asyncio
async def test_enqueue_calls_handler_validate_when_registered():
    captured: list[Any] = []

    def validator(payload):
        captured.append(payload)
        return payload

    register(_FakeHandler(WorkerJobKind.TRIAL, payload_validator=validator))

    session = _FakeSession()
    await enqueue_worker_job(
        session,
        EnqueueRequest(
            kind=WorkerJobKind.TRIAL,
            queue_key="openai/gpt-5",
            payload={"trial_id": "t-1", "agent": "claude-code"},
        ),
    )

    assert captured == [{"trial_id": "t-1", "agent": "claude-code"}]


@pytest.mark.asyncio
async def test_enqueue_skips_validation_when_opted_out():
    calls: list[Any] = []
    register(
        _FakeHandler(
            WorkerJobKind.TRIAL,
            payload_validator=lambda payload: calls.append(payload) or payload,
        )
    )
    session = _FakeSession()

    await enqueue_worker_job(
        session,
        EnqueueRequest(
            kind=WorkerJobKind.TRIAL,
            queue_key="default",
            payload={"pre": "validated"},
        ),
        validate=False,
    )

    assert calls == []


@pytest.mark.asyncio
async def test_enqueue_tolerates_missing_handler():
    # Phase B has no handlers registered -- enqueue must still work so
    # dual-write can begin before handlers land.
    session = _FakeSession()
    row = await enqueue_worker_job(
        session,
        EnqueueRequest(
            kind=WorkerJobKind.TRIAL,
            queue_key="default",
            payload={"anything": True},
        ),
    )
    assert row.kind == WorkerJobKind.TRIAL


# ---------------------------------------------------------------------------
# Claim SQL invariants
# ---------------------------------------------------------------------------


def _normalized_claim_sql() -> str:
    # Collapse runs of whitespace so layout tweaks (the SQL is aligned
    # with multiple spaces between keywords) don't break these checks.
    import re

    return re.sub(r"\s+", " ", _CLAIM_WORKER_JOB_SQL).strip()


def test_claim_sql_uses_skip_locked():
    # ``FOR UPDATE OF wj SKIP LOCKED`` scopes the lock to the claim
    # CTE alias so the fairness JOIN doesn't accidentally lock unrelated
    # ``trials`` / ``tasks`` rows.
    assert "FOR UPDATE OF wj SKIP LOCKED" in _normalized_claim_sql()


def test_claim_sql_filters_to_queued_and_retrying():
    # The claim path must ignore terminal / cancelled / blocked rows.
    assert "('QUEUED', 'RETRYING')" in _normalized_claim_sql()


def test_claim_sql_respects_available_after_gate():
    assert "available_after <= NOW()" in _normalized_claim_sql()


def test_claim_sql_orders_by_priority_desc_then_created():
    # The fairness subquery inserts ``COALESCE(rpg.running_count, 0)``
    # between priority and created_at so the least-loaded user wins
    # ties among TRIAL rows without affecting other kinds (where the
    # join degenerates and running_count is 0).
    sql = _normalized_claim_sql()
    assert (
        "ORDER BY wj.priority DESC, COALESCE(rpg.running_count, 0) ASC, wj.created_at ASC"
        in sql
    )


def test_claim_sql_increments_attempts_and_stamps_claim_metadata():
    sql = _normalized_claim_sql()
    for needle in (
        "status = 'RUNNING'",
        "attempts = attempts + 1",
        "claimed_at = NOW()",
        "heartbeat_at = NOW()",
        "current_worker_id = $2",
        "current_queue_slot = $3",
        "modal_function_call_id = $4",
    ):
        assert needle in sql, f"missing: {needle}"


def test_claim_sql_clears_retry_timestamp_on_claim():
    assert "next_retry_at = NULL" in _normalized_claim_sql()


# ---------------------------------------------------------------------------
# retry backoff helpers
# ---------------------------------------------------------------------------


def test_trial_retry_backoff_uses_exponential_delay_and_jitter():
    delay = worker_job_single_job.calculate_trial_retry_delay_seconds(
        attempts=3,
        error_message="transient agent failure",
        jitter=0.25,
    )

    assert delay == 150.0


def test_trial_retry_backoff_uses_longer_rate_limit_base():
    delay = worker_job_single_job.calculate_trial_retry_delay_seconds(
        attempts=1,
        error_message="Gemini failed with HTTP 429: rate limit exceeded",
        jitter=0.0,
    )

    assert delay == 300.0
    assert (
        worker_job_single_job.classify_retry_reason("RESOURCE_EXHAUSTED quota")
        == "rate_limit"
    )


def test_trial_retry_backoff_is_capped_after_jitter():
    delay = worker_job_single_job.calculate_trial_retry_delay_seconds(
        attempts=10,
        error_message="rate limit exceeded",
        jitter=0.25,
    )

    assert delay == worker_job_single_job.TRIAL_RETRY_MAX_DELAY_SECONDS


class _FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.closed = False

    async def execute(self, sql: str, *args: Any) -> None:
        self.calls.append((sql, args))

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_record_outcome_requeues_trial_with_backoff_and_mirrors_next_retry(
    monkeypatch,
):
    connection = _FakeConnection()

    async def fake_open_connection():
        return connection

    monkeypatch.setattr(
        worker_job_single_job, "_open_connection", fake_open_connection
    )
    monkeypatch.setattr(worker_job_single_job.random, "uniform", lambda _a, _b: 0.0)

    before = datetime.now(timezone.utc)
    await worker_job_single_job._record_outcome(
        job_id="wj-1",
        outcome=JobOutcome.fail("HTTP 503 from agent", retryable=True),
        attempts=2,
        max_attempts=6,
        kind=WorkerJobKind.TRIAL,
        subject_table="trials",
        subject_id="trial-1",
    )
    after = datetime.now(timezone.utc)

    assert connection.closed is True
    assert len(connection.calls) == 2

    worker_sql, worker_args = connection.calls[0]
    assert "status = 'RETRYING'" in worker_sql
    assert "next_retry_at = $3" in worker_sql
    assert "available_after = COALESCE($3::timestamptz, NOW())" in worker_sql
    assert worker_args[0] == "wj-1"
    assert worker_args[1] == "HTTP 503 from agent"

    retry_at = worker_args[2]
    assert retry_at is not None
    assert (
        before + timedelta(seconds=60)
        <= retry_at
        <= after + timedelta(seconds=60)
    )

    trial_sql, trial_args = connection.calls[1]
    assert "UPDATE trials" in trial_sql
    assert "next_retry_at = $2" in trial_sql
    assert trial_args == ("trial-1", retry_at)


# ---------------------------------------------------------------------------
# run_single_worker_job: dispatch + outcome recording
# ---------------------------------------------------------------------------


def _install_fake_claim(monkeypatch, job: ClaimedWorkerJob | None):
    async def fake_claim(
        queue_key, *, worker_id, queue_slot, modal_function_call_id=None
    ):
        return job

    monkeypatch.setattr(worker_job_single_job, "claim_single_worker_job", fake_claim)


def _capture_record_outcome(monkeypatch):
    captured: list[dict[str, Any]] = []

    async def fake_record(
        *,
        job_id,
        outcome,
        attempts,
        max_attempts,
        kind=None,
        subject_table=None,
        subject_id=None,
    ):
        captured.append(
            {
                "job_id": job_id,
                "outcome": outcome,
                "attempts": attempts,
                "max_attempts": max_attempts,
                "kind": kind,
                "subject_table": subject_table,
                "subject_id": subject_id,
            }
        )

    monkeypatch.setattr(worker_job_single_job, "_record_outcome", fake_record)
    return captured


def _make_claimed(
    *,
    kind: WorkerJobKind = WorkerJobKind.QA_REVIEW,
    attempts: int = 1,
    max_attempts: int = 3,
) -> ClaimedWorkerJob:
    return ClaimedWorkerJob(
        id="wj-1",
        kind=kind,
        queue_key="default",
        subject_table="trials",
        subject_id="t-1",
        payload={"trial_id": "t-1"},
        attempts=attempts,
        max_attempts=max_attempts,
        org_id=None,
        parent_job_id=None,
    )


@pytest.mark.asyncio
async def test_run_single_worker_job_returns_false_when_queue_empty(monkeypatch):
    _install_fake_claim(monkeypatch, None)
    captured = _capture_record_outcome(monkeypatch)

    result = await worker_job_single_job.run_single_worker_job(
        "default", worker_id="w-1", queue_slot=0
    )

    assert result is False
    assert captured == []


@pytest.mark.asyncio
async def test_run_single_worker_job_records_success(monkeypatch):
    job = _make_claimed()
    handler = _FakeHandler(job.kind, outcome=JobOutcome.ok({"answer": 42}))
    register(handler)

    _install_fake_claim(monkeypatch, job)
    captured = _capture_record_outcome(monkeypatch)

    result = await worker_job_single_job.run_single_worker_job(
        "default", worker_id="w-1", queue_slot=0
    )

    assert result is True
    assert handler.run_calls == [job]
    assert len(captured) == 1
    recorded = captured[0]
    assert recorded["job_id"] == "wj-1"
    assert recorded["outcome"].success is not None
    assert recorded["outcome"].success.result_summary == {"answer": 42}


@pytest.mark.asyncio
async def test_run_single_worker_job_records_retryable_on_exception(monkeypatch):
    job = _make_claimed(attempts=2, max_attempts=5)
    handler = _FakeHandler(job.kind, raise_exc=RuntimeError("boom"))
    register(handler)

    _install_fake_claim(monkeypatch, job)
    captured = _capture_record_outcome(monkeypatch)

    await worker_job_single_job.run_single_worker_job(
        "default", worker_id="w-1", queue_slot=0
    )

    recorded = captured[0]
    assert recorded["outcome"].failure is not None
    assert recorded["outcome"].failure.retryable is True
    assert "RuntimeError" in recorded["outcome"].failure.error_message


@pytest.mark.asyncio
async def test_run_single_worker_job_handles_missing_handler(monkeypatch):
    job = _make_claimed()
    # Nothing registered.

    _install_fake_claim(monkeypatch, job)
    captured = _capture_record_outcome(monkeypatch)

    result = await worker_job_single_job.run_single_worker_job(
        "default", worker_id="w-1", queue_slot=0
    )

    assert result is True
    recorded = captured[0]
    assert recorded["outcome"].failure is not None
    # No-handler failures are permanent -- retrying can't help.
    assert recorded["outcome"].failure.retryable is False


@pytest.mark.asyncio
async def test_run_single_worker_job_propagates_cancellation(monkeypatch):
    job = _make_claimed()
    handler = _FakeHandler(job.kind, raise_exc=asyncio.CancelledError())
    register(handler)

    _install_fake_claim(monkeypatch, job)
    _capture_record_outcome(monkeypatch)

    with pytest.raises(asyncio.CancelledError):
        await worker_job_single_job.run_single_worker_job(
            "default", worker_id="w-1", queue_slot=0
        )


@pytest.mark.asyncio
async def test_run_single_worker_job_rejects_invalid_outcome(monkeypatch):
    # If a handler returns both success+failure unset, the runner should
    # coerce it into a non-retryable failure rather than leave the row
    # RUNNING forever.
    job = _make_claimed()

    class _NaughtyHandler(_FakeHandler):
        async def run(self, job):  # type: ignore[override]
            outcome = JobOutcome.ok()
            object.__setattr__(outcome, "success", None)  # break the invariant
            return outcome

    register(_NaughtyHandler(job.kind))

    _install_fake_claim(monkeypatch, job)
    captured = _capture_record_outcome(monkeypatch)

    await worker_job_single_job.run_single_worker_job(
        "default", worker_id="w-1", queue_slot=0
    )

    recorded = captured[0]
    assert recorded["outcome"].failure is not None
    assert recorded["outcome"].failure.retryable is False


# ---------------------------------------------------------------------------
# ClaimedWorkerJob shape
# ---------------------------------------------------------------------------


def test_claimed_worker_job_fields_match_schema_expectations():
    job = _make_claimed()
    # Locked-down shape -- dispatcher/downstream code relies on these
    # keys existing. Any rename here needs a coordinated change. The
    # ``worker_id`` / ``queue_slot`` / ``modal_function_call_id``
    # fields are populated from the dispatcher's call-site values
    # rather than read back from the DB.
    assert set(asdict(job)) == {
        "id",
        "kind",
        "queue_key",
        "subject_table",
        "subject_id",
        "payload",
        "attempts",
        "max_attempts",
        "org_id",
        "parent_job_id",
        "worker_id",
        "queue_slot",
        "modal_function_call_id",
    }

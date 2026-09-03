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
import types
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
from oddish.core.harbor_artifacts import (  # noqa: E402
    THUNDER_CAPACITY_UNAVAILABLE_CODE,
)
from oddish.config import settings  # noqa: E402
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


def test_claim_sql_blocks_reroute_until_source_teardown_is_confirmed():
    assert "NOT wj.reroute_pending_teardown" in _normalized_claim_sql()


def test_claim_sql_scopes_to_harbor_variant():
    # harbor_variant_id is part of the effective dispatch key: a worker only
    # claims rows of the variant it was spawned for (default + ephemeral on the
    # default image, blessed ids on their own image). Both the claim predicate
    # and the per-user fairness count are scoped to $5.
    sql = _normalized_claim_sql()
    assert "wj.harbor_variant_id = $5" in sql
    assert "wj2.harbor_variant_id = $5" in sql


def test_claim_sql_returns_harbor_variant():
    assert "harbor_variant_id" in _normalized_claim_sql().split("RETURNING", 1)[1]


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


def test_trial_retry_backoff_honors_harbor_retry_after_hint():
    delay = worker_job_single_job.calculate_trial_retry_delay_seconds(
        attempts=1,
        error_message="provider overloaded",
        jitter=0.0,
        retry_after_seconds=90.0,
    )

    assert delay == 90.0


class _FakeConnection:
    def __init__(self, *, update_result: str = "UPDATE 1") -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.closed = False
        self.update_result = update_result

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append((sql, args))
        return self.update_result

    async def fetchrow(self, sql: str, *args: Any) -> None:
        # The retry decision re-reads the current row; None exercises the
        # snapshot fallback so these tests keep their original semantics.
        self.calls.append((sql, args))
        return None

    async def close(self) -> None:
        self.closed = True


class _FakeTransaction:
    def __init__(self) -> None:
        self.entered = False
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, _exc, _tb):
        self.rolled_back = exc_type is not None
        self.committed = exc_type is None


class _RerouteConnection:
    def __init__(
        self,
        *,
        job_overrides: dict[str, Any] | None = None,
        trial_overrides: dict[str, Any] | None = None,
        run_overrides: dict[str, Any] | None = None,
        leases: list[dict[str, Any]] | None = None,
        failed_update: str | None = None,
    ) -> None:
        self.job = {
            "id": "wj-1",
            "kind": "TRIAL",
            "status": "RUNNING",
            "subject_table": "trials",
            "subject_id": "trial-1",
            "attempts": 3,
            "current_worker_id": "worker-1",
            "execution_lane": "thunder_trial",
            "provider": None,
            "external_id": None,
            **(job_overrides or {}),
        }
        self.trial = {
            "id": "trial-1",
            "status": "RUNNING",
            "environment": "thunder",
            "attempts": 9,
            "current_worker_id": "worker-1",
            "deleted_at": None,
            "superseded_by_trial_id": None,
            **(trial_overrides or {}),
        }
        self.run = {
            "id": "sandbox-run-1",
            "state": "FAILED",
            "provider": "thunder",
            "external_id": None,
            "worker_job_attempt": 3,
            "trial_id": "trial-1",
            "deleted_at": None,
            **(run_overrides or {}),
        }
        self.leases = (
            leases
            if leases is not None
            else [
                {
                    "provider": "thunder",
                    "slot": 7,
                    "locked_by": "worker-1",
                    "worker_job_id": "wj-1",
                }
            ]
        )
        self.failed_update = failed_update
        self.transaction_state = _FakeTransaction()
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.closed = False

    def transaction(self):
        return self.transaction_state

    async def fetchrow(self, sql: str, *args: Any):
        self.calls.append((sql, args))
        if "FROM worker_jobs" in sql:
            return self.job
        if "FROM trials" in sql:
            return self.trial
        if "FROM sandbox_runs" in sql:
            return self.run
        raise AssertionError(sql)

    async def fetch(self, sql: str, *args: Any):
        self.calls.append((sql, args))
        assert "FROM sandbox_capacity_leases" in sql
        return self.leases

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append((sql, args))
        if self.failed_update and self.failed_update in sql:
            return "UPDATE 0"
        return "UPDATE 1"

    async def close(self) -> None:
        self.closed = True


def _reroute_outcome(*, target: str = "modal") -> JobOutcome:
    return JobOutcome.reroute_to(
        target_environment=target,
        target_execution_lane="default",
        reason=THUNDER_CAPACITY_UNAVAILABLE_CODE,
        retry_after_seconds=20.0,
        subject_attempt=9,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("target_provider", ["modal", "daytona"])
async def test_record_outcome_atomically_reroutes_unprovisioned_thunder_attempt(
    monkeypatch,
    target_provider,
):
    connection = _RerouteConnection()
    monkeypatch.setattr(settings, "thunder_capacity_fallback", True)
    monkeypatch.setattr(settings, "thunder_fallback_provider", target_provider)

    async def fake_open_connection():
        return connection

    monkeypatch.setattr(worker_job_single_job, "_open_connection", fake_open_connection)

    status = await worker_job_single_job._record_outcome(
        job_id="wj-1",
        worker_id="worker-1",
        outcome=_reroute_outcome(target=target_provider),
        attempts=3,
        max_attempts=3,
        kind=WorkerJobKind.TRIAL,
        subject_table="trials",
        subject_id="trial-1",
    )

    assert status == WorkerJobStatus.RETRYING
    assert connection.closed is True
    assert connection.transaction_state.committed is True
    update_calls = [call for call in connection.calls if "UPDATE " in call[0]]
    assert [
        next(
            table
            for table in (
                "sandbox_runs",
                "trials",
                "worker_jobs",
                "sandbox_capacity_leases",
            )
            if f"UPDATE {table}" in sql
        )
        for sql, _args in update_calls
    ] == [
        "sandbox_runs",
        "trials",
        "worker_jobs",
        "sandbox_capacity_leases",
    ]

    trial_sql, trial_args = update_calls[1]
    assert trial_args == ("trial-1", target_provider, "worker-1")
    assert "environment = $2" in trial_sql
    assert "status = 'RETRYING'" in trial_sql
    trial_set_clause = trial_sql.split("SET", 1)[1].split("WHERE", 1)[0]
    for preserved_column in (
        "attempts",
        "agent",
        "model",
        "queue_key",
        "harbor_config",
        "reward",
        "result",
        "error_message",
    ):
        assert f"{preserved_column} =" not in trial_set_clause

    job_sql, job_args = update_calls[2]
    assert job_args == (
        "wj-1",
        "default",
        "worker-1",
        3,
        False,
        THUNDER_CAPACITY_UNAVAILABLE_CODE,
    )
    assert "execution_lane = $2" in job_sql
    assert "status = 'RETRYING'" in job_sql
    assert "available_after = NOW()" in job_sql
    assert "reroute_from_environment = 'thunder'" in job_sql
    assert "reroute_pending_teardown = $5" in job_sql
    job_set_clause = job_sql.split("SET", 1)[1].split("WHERE", 1)[0]
    for preserved_column in (
        "attempts",
        "max_attempts",
        "queue_key",
        "harbor_variant_id",
        "payload",
        "priority",
        "error_message",
        "result_summary",
    ):
        assert f"{preserved_column} =" not in job_set_clause


@pytest.mark.asyncio
async def test_reroute_waits_for_confirmed_external_sandbox_teardown(monkeypatch):
    connection = _RerouteConnection(
        job_overrides={"provider": "thunder", "external_id": "sandbox-123"},
        run_overrides={
            "state": "TERMINATING",
            "external_id": "sandbox-123",
        },
    )
    monkeypatch.setattr(settings, "thunder_capacity_fallback", True)
    monkeypatch.setattr(settings, "thunder_fallback_provider", "modal")
    handoff_events = []
    monkeypatch.setattr(
        worker_job_single_job,
        "_emit_thunder_handoff_event",
        lambda outcome, **kwargs: handoff_events.append((outcome, kwargs["reason"])),
    )

    async def fake_open_connection():
        return connection

    monkeypatch.setattr(worker_job_single_job, "_open_connection", fake_open_connection)

    status = await worker_job_single_job._record_outcome(
        job_id="wj-1",
        worker_id="worker-1",
        outcome=_reroute_outcome(),
        attempts=3,
        max_attempts=6,
        kind=WorkerJobKind.TRIAL,
        subject_table="trials",
        subject_id="trial-1",
    )

    assert status == WorkerJobStatus.RETRYING
    updates = [sql for sql, _args in connection.calls if "UPDATE " in sql]
    assert len(updates) == 2
    assert "UPDATE trials" in updates[0]
    assert "UPDATE worker_jobs" in updates[1]
    job_args = next(
        args for sql, args in connection.calls if "UPDATE worker_jobs" in sql
    )
    assert job_args[-2:] == (True, THUNDER_CAPACITY_UNAVAILABLE_CODE)
    assert "CASE WHEN $5 THEN provider ELSE NULL END" in updates[1]
    assert handoff_events == [("pending", "teardown_pending")]


@pytest.mark.asyncio
async def test_reroute_releases_confirmed_external_sandbox(monkeypatch):
    connection = _RerouteConnection(
        job_overrides={"provider": "thunder", "external_id": "sandbox-123"},
        run_overrides={"state": "TERMINATED", "external_id": "sandbox-123"},
    )
    monkeypatch.setattr(settings, "thunder_capacity_fallback", True)
    monkeypatch.setattr(settings, "thunder_fallback_provider", "modal")

    async def fake_open_connection():
        return connection

    monkeypatch.setattr(worker_job_single_job, "_open_connection", fake_open_connection)

    status = await worker_job_single_job._record_outcome(
        job_id="wj-1",
        worker_id="worker-1",
        outcome=_reroute_outcome(),
        attempts=3,
        max_attempts=6,
        kind=WorkerJobKind.TRIAL,
        subject_table="trials",
        subject_id="trial-1",
    )

    assert status == WorkerJobStatus.RETRYING
    updates = [sql for sql, _args in connection.calls if "UPDATE " in sql]
    assert len(updates) == 3
    assert not any("UPDATE sandbox_runs" in sql for sql in updates)
    assert "UPDATE sandbox_capacity_leases" in updates[-1]
    job_args = next(
        args for sql, args in connection.calls if "UPDATE worker_jobs" in sql
    )
    assert job_args[-2:] == (False, THUNDER_CAPACITY_UNAVAILABLE_CODE)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "connection",
    [
        _RerouteConnection(job_overrides={"status": "CANCELLED"}),
        _RerouteConnection(trial_overrides={"current_worker_id": "other-worker"}),
        _RerouteConnection(run_overrides={"external_id": "sandbox-123"}),
        _RerouteConnection(leases=[]),
    ],
)
async def test_record_outcome_rejects_reroute_when_owned_state_changed(
    monkeypatch,
    connection,
):
    monkeypatch.setattr(settings, "thunder_capacity_fallback", True)
    monkeypatch.setattr(settings, "thunder_fallback_provider", "modal")

    async def fake_open_connection():
        return connection

    monkeypatch.setattr(worker_job_single_job, "_open_connection", fake_open_connection)

    status = await worker_job_single_job._record_outcome(
        job_id="wj-1",
        worker_id="worker-1",
        outcome=_reroute_outcome(),
        attempts=3,
        max_attempts=6,
        kind=WorkerJobKind.TRIAL,
        subject_table="trials",
        subject_id="trial-1",
    )

    assert status is None
    assert connection.closed is True
    assert not any("UPDATE " in sql for sql, _args in connection.calls)


@pytest.mark.asyncio
async def test_record_outcome_rolls_back_if_atomic_reroute_update_loses_ownership(
    monkeypatch,
):
    connection = _RerouteConnection(failed_update="UPDATE worker_jobs")
    monkeypatch.setattr(settings, "thunder_capacity_fallback", True)
    monkeypatch.setattr(settings, "thunder_fallback_provider", "modal")

    async def fake_open_connection():
        return connection

    monkeypatch.setattr(worker_job_single_job, "_open_connection", fake_open_connection)

    with pytest.raises(RuntimeError, match="worker-job ownership"):
        await worker_job_single_job._record_outcome(
            job_id="wj-1",
            worker_id="worker-1",
            outcome=_reroute_outcome(),
            attempts=3,
            max_attempts=6,
            kind=WorkerJobKind.TRIAL,
            subject_table="trials",
            subject_id="trial-1",
        )

    assert connection.transaction_state.rolled_back is True
    assert connection.closed is True


@pytest.mark.asyncio
async def test_record_outcome_requeues_trial_with_backoff_and_mirrors_next_retry(
    monkeypatch,
):
    connection = _FakeConnection()

    async def fake_open_connection():
        return connection

    monkeypatch.setattr(worker_job_single_job, "_open_connection", fake_open_connection)
    monkeypatch.setattr(worker_job_single_job.random, "uniform", lambda _a, _b: 0.0)

    before = datetime.now(timezone.utc)
    status = await worker_job_single_job._record_outcome(
        job_id="wj-1",
        worker_id="w-test",
        outcome=JobOutcome.fail("HTTP 503 from agent", retryable=True),
        attempts=2,
        max_attempts=6,
        kind=WorkerJobKind.TRIAL,
        subject_table="trials",
        subject_id="trial-1",
    )
    after = datetime.now(timezone.utc)

    assert status == WorkerJobStatus.RETRYING
    assert connection.closed is True
    assert len(connection.calls) == 3

    # The retry decision re-reads the current row before choosing.
    reread_sql, reread_args = connection.calls[0]
    assert "SELECT attempts, max_attempts" in reread_sql
    assert reread_args == ("wj-1",)

    worker_sql, worker_args = connection.calls[1]
    assert "status = 'RETRYING'" in worker_sql
    # The retry row must start UNLINKED: a kept handle can point at a pod that
    # still exists, blinding the orphan sweeper's live-unlinked guard while the
    # next attempt's pod is still unreferenced.
    assert "external_id = NULL" in worker_sql
    assert "provider = NULL" in worker_sql
    assert "next_retry_at = $3" in worker_sql
    assert "available_after = COALESCE($3::timestamptz, NOW())" in worker_sql
    assert worker_args[0] == "wj-1"
    assert worker_args[1] == "HTTP 503 from agent"

    retry_at = worker_args[2]
    assert retry_at is not None
    assert before + timedelta(seconds=60) <= retry_at <= after + timedelta(seconds=60)

    trial_sql, trial_args = connection.calls[2]
    assert "UPDATE trials" in trial_sql
    assert "status = 'RETRYING'" in trial_sql
    assert "error_message = $2" in trial_sql
    assert "next_retry_at = $3" in trial_sql
    assert "current_worker_id = NULL" in trial_sql
    assert "current_queue_slot = NULL" in trial_sql
    assert trial_args == ("trial-1", "HTTP 503 from agent", retry_at)


@pytest.mark.asyncio
async def test_record_outcome_returns_success_only_when_guarded_update_changes_row(
    monkeypatch,
):
    accepted_connection = _FakeConnection()

    async def accepted_open_connection():
        return accepted_connection

    monkeypatch.setattr(
        worker_job_single_job, "_open_connection", accepted_open_connection
    )
    accepted = await worker_job_single_job._record_outcome(
        job_id="wj-1",
        worker_id="w-test",
        outcome=JobOutcome.ok(),
        attempts=1,
        max_attempts=3,
    )

    rejected_connection = _FakeConnection(update_result="UPDATE 0")

    async def rejected_open_connection():
        return rejected_connection

    monkeypatch.setattr(
        worker_job_single_job, "_open_connection", rejected_open_connection
    )
    rejected = await worker_job_single_job._record_outcome(
        job_id="wj-1",
        worker_id="w-test",
        outcome=JobOutcome.ok(),
        attempts=1,
        max_attempts=3,
    )

    assert accepted == WorkerJobStatus.SUCCESS
    assert rejected is None
    assert accepted_connection.closed is True
    assert rejected_connection.closed is True


# ---------------------------------------------------------------------------
# run_single_worker_job: dispatch + outcome recording
# ---------------------------------------------------------------------------


def _install_fake_claim(monkeypatch, job: ClaimedWorkerJob | None):
    async def fake_claim(
        queue_key,
        *,
        worker_id,
        queue_slot,
        modal_function_call_id=None,
        harbor_variant_id="default",
    ):
        return job

    monkeypatch.setattr(worker_job_single_job, "claim_single_worker_job", fake_claim)


def _capture_record_outcome(monkeypatch):
    captured: list[dict[str, Any]] = []

    async def fake_record(
        *,
        job_id,
        worker_id,
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
                "worker_id": worker_id,
                "outcome": outcome,
                "attempts": attempts,
                "max_attempts": max_attempts,
                "kind": kind,
                "subject_table": subject_table,
                "subject_id": subject_id,
            }
        )
        if outcome.success is not None:
            return WorkerJobStatus.SUCCESS
        if outcome.reroute is not None:
            return WorkerJobStatus.RETRYING
        if outcome.failure.retryable and attempts < max_attempts:
            return WorkerJobStatus.RETRYING
        return WorkerJobStatus.FAILED

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
    job = _make_claimed(kind=WorkerJobKind.TRIAL)
    handler = _FakeHandler(job.kind, outcome=JobOutcome.ok({"answer": 42}))
    register(handler)

    _install_fake_claim(monkeypatch, job)
    captured = _capture_record_outcome(monkeypatch)
    metric_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        worker_job_single_job,
        "record_worker_job_transition",
        lambda **values: metric_calls.append(values),
    )
    monotonic_values = iter((10.0, 15.0))
    monkeypatch.setattr(
        worker_job_single_job,
        "time",
        types.SimpleNamespace(monotonic=lambda: next(monotonic_values)),
    )

    result = await worker_job_single_job.run_single_worker_job(
        "default", worker_id="w-1", queue_slot=0
    )

    assert result is True
    assert handler.run_calls == [job]
    assert len(captured) == 1
    recorded = captured[0]
    assert recorded["job_id"] == "wj-1"
    assert recorded["worker_id"] == "w-1"
    assert recorded["outcome"].success is not None
    assert recorded["outcome"].success.result_summary == {"answer": 42}
    assert len(metric_calls) == 1
    assert metric_calls[0]["kind"] == WorkerJobKind.TRIAL
    assert metric_calls[0]["outcome"] == WorkerJobStatus.SUCCESS
    assert metric_calls[0]["duration_seconds"] == 5.0


@pytest.mark.asyncio
async def test_run_single_worker_job_records_retryable_on_exception(monkeypatch):
    job = _make_claimed(kind=WorkerJobKind.TRIAL, attempts=2, max_attempts=5)
    handler = _FakeHandler(job.kind, raise_exc=RuntimeError("boom"))
    register(handler)

    _install_fake_claim(monkeypatch, job)
    captured = _capture_record_outcome(monkeypatch)
    metric_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        worker_job_single_job,
        "record_worker_job_transition",
        lambda **values: metric_calls.append(values),
    )

    await worker_job_single_job.run_single_worker_job(
        "default", worker_id="w-1", queue_slot=0
    )

    recorded = captured[0]
    assert recorded["outcome"].failure is not None
    assert recorded["outcome"].failure.retryable is True
    assert "RuntimeError" in recorded["outcome"].failure.error_message
    assert [call["outcome"] for call in metric_calls] == [WorkerJobStatus.RETRYING]


@pytest.mark.asyncio
async def test_run_single_worker_job_preserves_reroute_disposition(monkeypatch):
    job = _make_claimed(kind=WorkerJobKind.TRIAL)
    handler = _FakeHandler(job.kind, outcome=_reroute_outcome())
    register(handler)
    _install_fake_claim(monkeypatch, job)
    captured = _capture_record_outcome(monkeypatch)

    await worker_job_single_job.run_single_worker_job(
        "default", worker_id="w-1", queue_slot=0
    )

    assert captured[0]["outcome"].reroute is not None
    assert captured[0]["outcome"].failure is None


@pytest.mark.asyncio
async def test_run_single_worker_job_emits_nothing_when_outcome_update_loses_race(
    monkeypatch,
):
    job = _make_claimed(kind=WorkerJobKind.TRIAL)
    register(_FakeHandler(job.kind, outcome=JobOutcome.ok()))
    _install_fake_claim(monkeypatch, job)

    async def rejected_outcome(**_values):
        return None

    metric_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(worker_job_single_job, "_record_outcome", rejected_outcome)
    monkeypatch.setattr(
        worker_job_single_job,
        "record_worker_job_transition",
        lambda **values: metric_calls.append(values),
    )

    result = await worker_job_single_job.run_single_worker_job(
        "default", worker_id="w-1", queue_slot=0
    )

    assert result is True
    assert metric_calls == []


@pytest.mark.asyncio
async def test_run_single_worker_job_handles_missing_handler(monkeypatch):
    job = _make_claimed()
    # Nothing registered.

    _install_fake_claim(monkeypatch, job)
    captured = _capture_record_outcome(monkeypatch)
    metric_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        worker_job_single_job,
        "record_worker_job_transition",
        lambda **values: metric_calls.append(values),
    )

    result = await worker_job_single_job.run_single_worker_job(
        "default", worker_id="w-1", queue_slot=0
    )

    assert result is True
    recorded = captured[0]
    assert recorded["outcome"].failure is not None
    # No-handler failures are permanent -- retrying can't help.
    assert recorded["outcome"].failure.retryable is False
    assert [call["outcome"] for call in metric_calls] == [WorkerJobStatus.FAILED]


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
        "harbor_variant_id",
        "execution_lane",
        "reroute_from_environment",
        "worker_id",
        "queue_slot",
        "modal_function_call_id",
        "claimed_at",
    }

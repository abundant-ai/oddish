"""Single-job runner over the unified `worker_jobs` table.

The only dispatcher path after the cutover from the legacy
per-kind claim SQLs. Kind-agnostic: claims one row with
``FOR UPDATE SKIP LOCKED`` and hands it to the registered
``JobHandler`` for the row's ``kind``.

All scheduling-state transitions (``QUEUED`` / ``RETRYING`` →
``RUNNING`` → ``SUCCESS`` / ``RETRYING`` / ``FAILED``) happen here.
Handlers still do their own domain writes (``trials.status``,
``tasks.verdict`` ...) inside ``JobHandler.run``; the runner only
touches ``worker_jobs``.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from oddish.config import settings
from oddish.core.harbor_artifacts import THUNDER_CAPACITY_UNAVAILABLE_CODE
from oddish.costs.recorder import (
    WorkerBillingSpec,
    close_worker_span,
    open_worker_span,
)
from oddish.db import WorkerJobKind, WorkerJobStatus
from oddish.observability import (
    record_thunder_capacity_handoff,
    record_worker_job_transition,
)
from oddish.runtime.sandbox_lifecycle import (
    DEFAULT_EXECUTION_LANE,
    THUNDER_TRIAL_EXECUTION_LANE,
    capacity_provider_for_execution_lane,
)
from oddish.workers.jobs.registry import (
    HANDLERS,
    JobOutcome,
    JobReroute,
    NoHandlerRegisteredError,
    get_handler,
)
from oddish.workers.queue.shared import console
from oddish.workers.queue.sandbox_capacity import SANDBOX_CAPACITY_LEASE_SECONDS

logger = logging.getLogger(__name__)


# Callback invoked after a claimed row completes successfully. Kept as a
# simple ``kind -> async fn(subject_id)`` dict so the backend can wire
# GitHub notifications (trial / analysis / verdict) without pushing
# backend-specific concerns into this module.
PostSuccessHooks = dict[WorkerJobKind, Callable[[str], Awaitable[None]]]

TRIAL_RETRY_BASE_DELAY_SECONDS = 30.0
TRIAL_RATE_LIMIT_RETRY_BASE_DELAY_SECONDS = 300.0
TRIAL_RETRY_MAX_DELAY_SECONDS = 1800.0
TRIAL_RETRY_JITTER_FRACTION = 0.25

_RATE_LIMIT_RE = re.compile(
    r"\b("
    r"429|"
    r"too many requests|"
    r"rate[\s_-]*limit(?:ed|s|ing)?|"
    r"ratelimit(?:ed|s|ing)?|"
    r"quota(?: exceeded)?|"
    r"resource[_\s-]*exhausted|"
    r"requests per minute|"
    r"tokens per minute|"
    r"throttl(?:ed|ing)?"
    r")\b",
    re.IGNORECASE,
)


def _ensure_handlers_registered() -> None:
    """Register built-in handlers lazily on first claim.

    The runner imports from ``oddish.workers.jobs.registry`` at module
    load (for JobOutcome / get_handler), but we defer pulling in the
    handler implementations until first use because ``handlers.py``
    imports back into this file for ``ClaimedWorkerJob``. Calling this
    at run time (by which point every module has finished initializing)
    breaks the cycle cleanly.
    """
    from oddish.workers.jobs import ensure_builtin_handlers_registered

    ensure_builtin_handlers_registered()


__all__ = [
    "ClaimedWorkerJob",
    "claim_single_worker_job",
    "run_single_worker_job",
    "drain_worker_jobs",
]


def classify_retry_reason(error_message: str | None) -> str:
    """Return a coarse retry reason for scheduling/telemetry."""
    if error_message and _RATE_LIMIT_RE.search(error_message):
        return "rate_limit"
    return "transient"


def calculate_trial_retry_delay_seconds(
    *,
    attempts: int,
    error_message: str | None,
    jitter: float | None = None,
    retry_after_seconds: float | None = None,
) -> float:
    """Return bounded exponential trial retry delay with multiplicative jitter.

    ``attempts`` is the attempt that just failed. A first failed attempt gets
    the base delay, the second gets 2x, and so on. Rate-limit-looking errors
    start at a higher base because immediately retrying usually makes the
    provider-side contention worse.
    """
    retry_reason = classify_retry_reason(error_message)
    base_delay = (
        TRIAL_RATE_LIMIT_RETRY_BASE_DELAY_SECONDS
        if retry_reason == "rate_limit"
        else TRIAL_RETRY_BASE_DELAY_SECONDS
    )
    exponential_delay = base_delay * (2 ** max(attempts - 1, 0))
    capped_delay = min(exponential_delay, TRIAL_RETRY_MAX_DELAY_SECONDS)
    jitter_value = (
        random.uniform(0.0, TRIAL_RETRY_JITTER_FRACTION) if jitter is None else jitter
    )
    jitter_value = max(0.0, min(jitter_value, TRIAL_RETRY_JITTER_FRACTION))
    delay = capped_delay * (1.0 + jitter_value)
    if retry_after_seconds is not None:
        delay = max(delay, retry_after_seconds)
    return float(min(delay, TRIAL_RETRY_MAX_DELAY_SECONDS))


# ---------------------------------------------------------------------------
# Claim SQL
#
# Single query replaces the three kind-specific claim SQLs in the
# legacy ``single_job.py``. Fair-scheduling-across-users for the
# TRIAL kind is expressed via a LEFT JOIN that degenerates to a no-op
# for every other kind, so the query is genuinely kind-agnostic at
# the surface:
#
#   - For TRIAL rows, the JOIN resolves the trial's fairness_key and
#     the subquery counts per-user RUNNING trials for this queue_key;
#     ORDER BY then prefers the least-loaded user.
#   - For non-TRIAL rows the JOINs produce NULLs and rpg.running_count
#     is 0 for every row, so ORDER BY collapses to
#     ``priority DESC, created_at ASC`` (plain FIFO with priority).
# ---------------------------------------------------------------------------
_CLAIM_WORKER_JOB_SQL = """
WITH capacity AS (
    SELECT provider, slot
    FROM sandbox_capacity_leases
    WHERE $7::text IS NOT NULL
      AND provider = $7
      AND slot = $8
      AND locked_by = $2
      AND worker_job_id IS NULL
      AND locked_until > NOW()
    FOR UPDATE
),
candidate AS (
    SELECT wj.id
    FROM   worker_jobs wj
    LEFT JOIN trials tr
        ON  wj.kind::text = 'TRIAL'
        AND wj.subject_table = 'trials'
        AND wj.subject_id = tr.id
    LEFT JOIN tasks tk ON tr.task_id = tk.id
    LEFT JOIN (
        SELECT COALESCE(tk2.created_by_user_id, tk2.user) AS fairness_key,
               COUNT(*) AS running_count
        FROM   worker_jobs wj2
        JOIN   trials tr2  ON wj2.subject_id = tr2.id
        JOIN   tasks  tk2  ON tr2.task_id = tk2.id
        WHERE  wj2.kind::text = 'TRIAL'
          AND  wj2.status::text = 'RUNNING'
          AND  wj2.queue_key = $1
          AND  ($5::text IS NULL OR wj2.harbor_variant_id = $5)
          AND  ($6::text IS NULL OR wj2.execution_lane = $6)
          AND  tr2.deleted_at IS NULL
          AND  tk2.deleted_at IS NULL
        GROUP  BY COALESCE(tk2.created_by_user_id, tk2.user)
    ) rpg ON rpg.fairness_key = COALESCE(tk.created_by_user_id, tk.user)
    WHERE  wj.queue_key = $1
      AND  ($5::text IS NULL OR wj.harbor_variant_id = $5)
      AND  ($6::text IS NULL OR wj.execution_lane = $6)
      AND  ($7::text IS NULL OR EXISTS (SELECT 1 FROM capacity))
      -- Only claim kinds this worker can actually run. Rows of retired
      -- kinds (or kinds added by a newer deploy) stay QUEUED instead of
      -- failing with "no handler registered". $10: $6-$9 are the
      -- execution-lane / sandbox-capacity params above.
      AND  wj.kind::text = ANY($10::text[])
      AND  wj.status::text IN ('QUEUED', 'RETRYING')
      AND  NOT wj.reroute_pending_teardown
      AND  wj.available_after <= NOW()
      AND  tr.deleted_at IS NULL
      AND  tk.deleted_at IS NULL
    ORDER  BY wj.priority DESC,
              COALESCE(rpg.running_count, 0) ASC,
              wj.created_at ASC
    LIMIT  1
    FOR    UPDATE OF wj SKIP LOCKED
),
claimed AS (
    UPDATE worker_jobs
    SET    status = 'RUNNING',
           claimed_at = NOW(),
           heartbeat_at = NOW(),
           attempts = attempts + 1,
           current_worker_id = $2,
           current_queue_slot = $3,
           modal_function_call_id = $4,
           started_at = COALESCE(started_at, NOW()),
           finished_at = NULL,
           next_retry_at = NULL
    WHERE id = (SELECT id FROM candidate)
    RETURNING id, kind::text AS kind, subject_table, subject_id, payload,
              attempts, max_attempts, queue_key, org_id, parent_job_id,
              harbor_variant_id, execution_lane, claimed_at,
              reroute_from_environment
),
bound_capacity AS (
    UPDATE sandbox_capacity_leases AS lease
    SET worker_job_id = claimed.id,
        locked_until = NOW() + make_interval(secs => $9)
    FROM claimed
    WHERE lease.provider = $7
      AND lease.slot = $8
      AND lease.locked_by = $2
      AND lease.worker_job_id IS NULL
    RETURNING lease.provider
)
SELECT claimed.*
FROM claimed
WHERE $7::text IS NULL OR EXISTS (SELECT 1 FROM bound_capacity);
"""


@dataclass(frozen=True)
class ClaimedWorkerJob:
    """Lightweight view of a claimed ``worker_jobs`` row.

    Kept minimal so the handler can hydrate a full ORM row if it wants
    more fields. The claim-metadata fields (``worker_id``,
    ``queue_slot``, ``modal_function_call_id``) are populated from the
    dispatcher's call-site values rather than read back from the DB --
    they were just written by the claim UPDATE.
    """

    id: str
    kind: WorkerJobKind
    queue_key: str
    subject_table: str | None
    subject_id: str | None
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    org_id: str | None
    parent_job_id: str | None
    harbor_variant_id: str = "default"
    execution_lane: str = "default"
    reroute_from_environment: str | None = None
    worker_id: str | None = None
    queue_slot: int | None = None
    modal_function_call_id: str | None = None
    claimed_at: datetime | None = None


async def _open_connection() -> asyncpg.Connection:
    return await asyncpg.connect(
        settings.asyncpg_url,
        statement_cache_size=0,
        server_settings=settings.asyncpg_server_settings(),
    )


class SandboxCapacityLeaseLostError(RuntimeError):
    """Raised when an EC2 worker no longer owns its required global lease."""


async def heartbeat_worker_job(
    job_id: str,
    *,
    current_worker_id: str | None = None,
    pending_failure_count: int = 0,
    pending_last_error: str | None = None,
) -> bool:
    """Update a RUNNING worker_job's heartbeat timestamp.

    Returns whether the worker still owns a running row. Terminal rows remain
    untouched so a late heartbeat cannot resurrect them.
    """
    connection = await _open_connection()
    try:
        if pending_failure_count > 0:
            await connection.execute(
                """
                UPDATE worker_jobs
                SET    heartbeat_at = NOW(),
                       heartbeat_failure_count = heartbeat_failure_count + $2,
                       last_heartbeat_error = $3,
                       last_heartbeat_error_at = NOW()
                WHERE  id = $1
                  AND  status::text = 'RUNNING'
                  AND  ($4::text IS NULL OR current_worker_id = $4)
                """,
                job_id,
                pending_failure_count,
                (pending_last_error or "")[:500] or None,
                current_worker_id,
            )
        else:
            await connection.execute(
                """
                UPDATE worker_jobs
                SET    heartbeat_at = NOW()
                WHERE  id = $1
                  AND  status::text = 'RUNNING'
                  AND  ($2::text IS NULL OR current_worker_id = $2)
                """,
                job_id,
                current_worker_id,
            )
        capacity_heartbeat = await connection.fetchrow(
            """
            WITH running_job AS (
                SELECT id, current_worker_id, execution_lane
                FROM worker_jobs
                WHERE id = $1
                  AND status::text = 'RUNNING'
                  AND ($2::text IS NULL OR current_worker_id = $2)
            ), renewed AS (
                UPDATE sandbox_capacity_leases AS lease
                SET locked_until = NOW() + make_interval(secs => $3)
                FROM running_job AS wj
                WHERE lease.worker_job_id = wj.id
                  AND lease.locked_by = wj.current_worker_id
                RETURNING lease.slot
            )
            SELECT EXISTS (SELECT 1 FROM running_job) AS still_owned,
                   (SELECT execution_lane FROM running_job) AS execution_lane,
                   EXISTS (SELECT 1 FROM renewed) AS capacity_renewed
            """,
            job_id,
            current_worker_id,
            SANDBOX_CAPACITY_LEASE_SECONDS,
        )
        heartbeat_lane = (
            capacity_heartbeat["execution_lane"]
            if capacity_heartbeat is not None
            else None
        )
        capacity_provider = capacity_provider_for_execution_lane(heartbeat_lane)
        if capacity_provider is not None and not capacity_heartbeat["capacity_renewed"]:
            raise SandboxCapacityLeaseLostError(
                f"{capacity_provider} worker_job {job_id} lost its global capacity lease"
            )
        return bool(capacity_heartbeat and capacity_heartbeat["still_owned"])
    finally:
        await connection.close()


async def claim_single_worker_job(
    queue_key: str,
    *,
    worker_id: str,
    queue_slot: int,
    modal_function_call_id: str | None = None,
    harbor_variant_id: str | None = "default",
    execution_lane: str | None = "default",
    capacity_provider: str | None = None,
    capacity_slot: int | None = None,
) -> ClaimedWorkerJob | None:
    """Atomically claim at most one runnable ``worker_jobs`` row.

    Returns ``None`` if no row was available. The claim is scoped to
    ``harbor_variant_id`` so a worker only picks up jobs of the Harbor variant
    it was spawned for -- except ``harbor_variant_id=None``, which claims **any**
    variant for the queue_key (used by the off-Modal / image-agnostic workers,
    which serve every variant of a queue_key with one worker). The returned row
    is in ``RUNNING`` state with ``attempts`` incremented and claim metadata
    stamped.
    """
    expected_capacity_provider = capacity_provider_for_execution_lane(execution_lane)
    if expected_capacity_provider is not None:
        if capacity_provider != expected_capacity_provider or capacity_slot is None:
            provider_label = expected_capacity_provider.upper()
            raise RuntimeError(
                f"{provider_label} trial claims require a pre-acquired "
                f"{provider_label} capacity lease"
            )
    elif capacity_provider is not None or capacity_slot is not None:
        raise RuntimeError(
            "sandbox capacity lease cannot be attached to an unbounded claim"
        )

    connection = await _open_connection()
    try:
        row = await connection.fetchrow(
            _CLAIM_WORKER_JOB_SQL,
            queue_key,
            worker_id,
            queue_slot,
            modal_function_call_id,
            harbor_variant_id,
            execution_lane,
            capacity_provider,
            capacity_slot,
            SANDBOX_CAPACITY_LEASE_SECONDS,
            sorted(kind.value for kind in HANDLERS),
        )
    finally:
        await connection.close()

    if row is None:
        return None

    raw_payload = row["payload"]
    if isinstance(raw_payload, str):
        # asyncpg returns JSONB as str unless a codec is registered on
        # this connection. Be defensive.
        import json

        payload = json.loads(raw_payload) if raw_payload else {}
    else:
        payload = dict(raw_payload or {})

    return ClaimedWorkerJob(
        id=str(row["id"]),
        kind=WorkerJobKind(row["kind"]),
        queue_key=str(row["queue_key"]),
        subject_table=row["subject_table"],
        subject_id=row["subject_id"],
        payload=payload,
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        org_id=row["org_id"],
        parent_job_id=row["parent_job_id"],
        harbor_variant_id=str(row["harbor_variant_id"]),
        execution_lane=str(row["execution_lane"]),
        reroute_from_environment=row.get("reroute_from_environment"),
        worker_id=worker_id,
        queue_slot=queue_slot,
        modal_function_call_id=modal_function_call_id,
        claimed_at=row.get("claimed_at"),
    )


def _updated_one(command: str) -> bool:
    return command.endswith(" 1")


def _emit_thunder_handoff_event(
    outcome: str,
    *,
    job_id: str,
    trial_id: str | None,
    target: str,
    reason: str,
) -> None:
    message = (
        f"metric=thunder_capacity_handoff outcome={outcome} "
        f"job_id={job_id} trial_id={trial_id or 'unknown'} "
        f"target={target} reason={reason}"
    )
    console.print(message)
    logger.info(message)
    record_thunder_capacity_handoff(
        outcome=outcome,  # type: ignore[arg-type]
        target_environment=target,
    )


async def _record_reroute_outcome(
    connection: asyncpg.Connection,
    *,
    job_id: str,
    worker_id: str,
    reroute: JobReroute,
    attempts: int,
    kind: WorkerJobKind | None,
    subject_table: str | None,
    subject_id: str | None,
) -> WorkerJobStatus | None:
    """Atomically hand an unprovisioned Thunder trial to a default-lane provider."""
    if (
        not settings.thunder_capacity_fallback
        or reroute.target_environment != settings.thunder_fallback_provider
        or kind != WorkerJobKind.TRIAL
        or subject_table != "trials"
        or not subject_id
        or reroute.target_execution_lane != DEFAULT_EXECUTION_LANE
        or reroute.reason != THUNDER_CAPACITY_UNAVAILABLE_CODE
        or reroute.subject_attempt is None
    ):
        _emit_thunder_handoff_event(
            "rejected",
            job_id=job_id,
            trial_id=subject_id,
            target=reroute.target_environment,
            reason="unsupported_disposition",
        )
        raise ValueError("unsupported worker-job reroute disposition")

    try:
        async with connection.transaction():
            job = await connection.fetchrow(
                """
            SELECT id,
                   kind::text AS kind,
                   status::text AS status,
                   subject_table,
                   subject_id,
                   attempts,
                   current_worker_id,
                   execution_lane,
                   provider,
                   external_id
            FROM worker_jobs
            WHERE id = $1
            FOR UPDATE
            """,
                job_id,
            )
            if (
                job is None
                or job["kind"] != WorkerJobKind.TRIAL.value
                or job["status"] != WorkerJobStatus.RUNNING.value
                or job["subject_table"] != "trials"
                or job["subject_id"] != subject_id
                or int(job["attempts"]) != attempts
                or job["current_worker_id"] != worker_id
                or job["execution_lane"] != THUNDER_TRIAL_EXECUTION_LANE
            ):
                _emit_thunder_handoff_event(
                    "rejected",
                    job_id=job_id,
                    trial_id=subject_id,
                    target=reroute.target_environment,
                    reason="worker_ownership_changed",
                )
                return None

            trial = await connection.fetchrow(
                """
            SELECT id,
                   status::text AS status,
                   environment,
                   attempts,
                   current_worker_id,
                   deleted_at,
                   superseded_by_trial_id
            FROM trials
            WHERE id = $1
            FOR UPDATE
            """,
                subject_id,
            )
            if (
                trial is None
                or trial["status"] != "RUNNING"
                or (trial["environment"] or "").strip().lower() != "thunder"
                or int(trial["attempts"]) != reroute.subject_attempt
                or trial["current_worker_id"] != worker_id
                or trial["deleted_at"] is not None
                or trial["superseded_by_trial_id"] is not None
            ):
                _emit_thunder_handoff_event(
                    "rejected",
                    job_id=job_id,
                    trial_id=subject_id,
                    target=reroute.target_environment,
                    reason="trial_ownership_changed",
                )
                return None

            sandbox_run = await connection.fetchrow(
                """
            SELECT id,
                   state,
                   provider,
                   external_id,
                   worker_job_attempt,
                   trial_id,
                   deleted_at
            FROM sandbox_runs
            WHERE worker_job_id = $1
              AND worker_job_attempt = $2
            FOR UPDATE
            """,
                job_id,
                attempts,
            )
            if (
                sandbox_run is None
                or sandbox_run["provider"] != "thunder"
                or int(sandbox_run["worker_job_attempt"]) != attempts
                or sandbox_run["trial_id"] != subject_id
                or sandbox_run["deleted_at"] is not None
                or sandbox_run["state"]
                not in {"PROVISIONING", "TERMINATING", "TERMINATED", "FAILED"}
            ):
                _emit_thunder_handoff_event(
                    "rejected",
                    job_id=job_id,
                    trial_id=subject_id,
                    target=reroute.target_environment,
                    reason="sandbox_ownership_changed",
                )
                return None

            sandbox_external_id = sandbox_run["external_id"]
            if sandbox_external_id is None:
                handle_matches = job["provider"] is None and job["external_id"] is None
            else:
                handle_matches = (
                    job["provider"] == "thunder"
                    and job["external_id"] == sandbox_external_id
                )
            if not handle_matches:
                _emit_thunder_handoff_event(
                    "rejected",
                    job_id=job_id,
                    trial_id=subject_id,
                    target=reroute.target_environment,
                    reason="provider_handle_mismatch",
                )
                return None

            capacity_leases = await connection.fetch(
                """
            SELECT provider, slot, locked_by, worker_job_id
            FROM sandbox_capacity_leases
            WHERE provider = 'thunder'
              AND worker_job_id = $1
              AND locked_by = $2
            FOR UPDATE
            """,
                job_id,
                worker_id,
            )
            if len(capacity_leases) != 1:
                _emit_thunder_handoff_event(
                    "rejected",
                    job_id=job_id,
                    trial_id=subject_id,
                    target=reroute.target_environment,
                    reason="capacity_lease_changed",
                )
                return None
            capacity_lease = capacity_leases[0]
            teardown_pending = bool(
                sandbox_external_id is not None and sandbox_run["state"] != "TERMINATED"
            )

            if sandbox_external_id is None:
                run_update = await connection.execute(
                    """
            UPDATE sandbox_runs
            SET state = 'TERMINATED',
                termination_requested_at = COALESCE(termination_requested_at, NOW()),
                terminated_at = COALESCE(terminated_at, NOW()),
                last_error = NULL
            WHERE id = $1
              AND external_id IS NULL
            """,
                    sandbox_run["id"],
                )
                if not _updated_one(run_update):
                    raise RuntimeError("Thunder reroute lost sandbox-run ownership")

            trial_update = await connection.execute(
                """
            UPDATE trials
            SET environment = $2,
                status = 'RETRYING',
                next_retry_at = NULL,
                current_worker_id = NULL,
                current_queue_slot = NULL,
                heartbeat_at = NOW()
            WHERE id = $1
              AND status::text = 'RUNNING'
              AND current_worker_id = $3
              AND deleted_at IS NULL
              AND superseded_by_trial_id IS NULL
            """,
                subject_id,
                reroute.target_environment,
                worker_id,
            )
            if not _updated_one(trial_update):
                raise RuntimeError("Thunder reroute lost trial ownership")

            job_update = await connection.execute(
                """
            UPDATE worker_jobs
            SET execution_lane = $2,
                status = 'RETRYING',
                next_retry_at = NULL,
                available_after = NOW(),
                current_worker_id = NULL,
                current_queue_slot = NULL,
                modal_function_call_id = NULL,
                provider = CASE WHEN $5 THEN provider ELSE NULL END,
                external_id = CASE WHEN $5 THEN external_id ELSE NULL END,
                reroute_from_environment = 'thunder',
                reroute_reason = $6,
                reroute_pending_teardown = $5
            WHERE id = $1
              AND status::text = 'RUNNING'
              AND current_worker_id = $3
              AND attempts = $4
              AND execution_lane = 'thunder_trial'
            """,
                job_id,
                reroute.target_execution_lane,
                worker_id,
                attempts,
                teardown_pending,
                reroute.reason,
            )
            if not _updated_one(job_update):
                raise RuntimeError("Thunder reroute lost worker-job ownership")

            if not teardown_pending:
                lease_update = await connection.execute(
                    """
            UPDATE sandbox_capacity_leases
            SET locked_by = NULL,
                worker_job_id = NULL,
                locked_at = NULL,
                locked_until = NULL
            WHERE provider = $1
              AND slot = $2
              AND locked_by = $3
              AND worker_job_id = $4
            """,
                    capacity_lease["provider"],
                    capacity_lease["slot"],
                    worker_id,
                    job_id,
                )
                if not _updated_one(lease_update):
                    raise RuntimeError("Thunder reroute lost capacity-lease ownership")
    except Exception as exc:
        _emit_thunder_handoff_event(
            "failed",
            job_id=job_id,
            trial_id=subject_id,
            target=reroute.target_environment,
            reason=type(exc).__name__,
        )
        raise

    if not teardown_pending:
        _emit_thunder_handoff_event(
            "completed",
            job_id=job_id,
            trial_id=subject_id,
            target=reroute.target_environment,
            reason=reroute.reason,
        )
    else:
        _emit_thunder_handoff_event(
            "failed",
            job_id=job_id,
            trial_id=subject_id,
            target=reroute.target_environment,
            reason="teardown_not_confirmed",
        )
    return WorkerJobStatus.RETRYING


async def _record_outcome(
    *,
    job_id: str,
    worker_id: str,
    outcome: JobOutcome,
    attempts: int,
    max_attempts: int,
    kind: WorkerJobKind | None = None,
    subject_table: str | None = None,
    subject_id: str | None = None,
) -> WorkerJobStatus | None:
    connection = await _open_connection()
    try:
        if outcome.reroute is not None:
            return await _record_reroute_outcome(
                connection,
                job_id=job_id,
                worker_id=worker_id,
                reroute=outcome.reroute,
                attempts=attempts,
                kind=kind,
                subject_table=subject_table,
                subject_id=subject_id,
            )
        if outcome.success is not None:
            import json

            summary = outcome.success.result_summary
            command = await connection.execute(
                """
                UPDATE worker_jobs
                SET    status = 'SUCCESS',
                       result_summary = $2::jsonb,
                       finished_at = NOW(),
                       heartbeat_at = NOW(),
                       next_retry_at = NULL,
                       error_message = NULL,
                       payload = payload - 'registry_auth_enc'
                WHERE  id = $1
                  AND  status = 'RUNNING'::worker_job_status
                  AND  current_worker_id = $3
                """,
                job_id,
                json.dumps(summary) if summary is not None else None,
                worker_id,
            )
            if not _updated_one(command):
                console.print(
                    f"[yellow]worker_job {job_id} outcome ignored; row is no longer RUNNING[/yellow]"
                )
                return None
            return WorkerJobStatus.SUCCESS

        assert outcome.failure is not None
        # Decide against the CURRENT row, not the claim-time snapshot: an
        # operator capping max_attempts (or a reaper racing) mid-attempt must
        # bind at this decision, or a surgically-capped trial schedules yet
        # another attempt from the worker's stale in-memory values.
        current = await connection.fetchrow(
            "SELECT attempts, max_attempts FROM worker_jobs WHERE id = $1",
            job_id,
        )
        if current is not None:
            attempts = int(current["attempts"])
            max_attempts = int(current["max_attempts"])
        retry = outcome.failure.retryable and attempts < max_attempts
        if retry:
            retry_at: datetime | None = None
            retry_reason = classify_retry_reason(outcome.failure.error_message)
            delay_seconds: float | None = None
            if kind == WorkerJobKind.TRIAL:
                delay_seconds = calculate_trial_retry_delay_seconds(
                    attempts=attempts,
                    error_message=outcome.failure.error_message,
                    retry_after_seconds=outcome.failure.retry_after_seconds,
                )
                retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)

            # RETRYING is a scheduling state, not a terminal one. Leave
            # finished_at NULL so the claim SQL can clear it on the
            # next attempt without special-casing; the duration query
            # already filters to SUCCESS/FAILED so it doesn't observe
            # RETRYING rows either way.
            command = await connection.execute(
                """
                UPDATE worker_jobs
                SET    status = 'RETRYING',
                       error_message = $2,
                       next_retry_at = $3,
                       available_after = COALESCE($3::timestamptz, NOW()),
                       current_worker_id = NULL,
                       current_queue_slot = NULL,
                       modal_function_call_id = NULL,
                       -- The retry starts UNLINKED (mirrors the reaper's retry
                       -- transition): a carried-over handle can point at a pod
                       -- that still exists, which blinds the orphan sweeper's
                       -- live-unlinked guard while the next attempt's pod is
                       -- unreferenced. This worker's own teardown already ran.
                       external_id = NULL,
                       provider = NULL
                WHERE  id = $1
                  AND  status = 'RUNNING'::worker_job_status
                  AND  current_worker_id = $4
                """,
                job_id,
                outcome.failure.error_message,
                retry_at,
                worker_id,
            )
            if not _updated_one(command):
                console.print(
                    f"[yellow]worker_job {job_id} retry outcome ignored; row is no longer RUNNING[/yellow]"
                )
                return None
            if (
                kind == WorkerJobKind.TRIAL
                and subject_table == "trials"
                and subject_id
                and retry_at is not None
            ):
                await connection.execute(
                    """
                    UPDATE trials
                    SET    status = 'RETRYING',
                           error_message = $2,
                           next_retry_at = $3,
                           current_worker_id = NULL,
                           current_queue_slot = NULL,
                           heartbeat_at = NOW()
                    WHERE  id = $1
                      AND  deleted_at IS NULL
                      AND  superseded_by_trial_id IS NULL
                    """,
                    subject_id,
                    outcome.failure.error_message,
                    retry_at,
                )
            console.print(
                f"metric=worker_job_retry_requeued id={job_id} "
                f"attempts={attempts}/{max_attempts} "
                f"retry_reason={retry_reason} "
                f"retry_delay_seconds={delay_seconds or 0:.2f}"
            )
            return WorkerJobStatus.RETRYING
        else:
            command = await connection.execute(
                """
                UPDATE worker_jobs
                SET    status = 'FAILED',
                       error_message = $2,
                       finished_at = NOW(),
                       next_retry_at = NULL,
                       payload = payload - 'registry_auth_enc'
                WHERE  id = $1
                  AND  status = 'RUNNING'::worker_job_status
                  AND  current_worker_id = $3
                """,
                job_id,
                outcome.failure.error_message,
                worker_id,
            )
            if not _updated_one(command):
                console.print(
                    f"[yellow]worker_job {job_id} failure outcome ignored; row is no longer RUNNING[/yellow]"
                )
                return None
            return WorkerJobStatus.FAILED
    finally:
        await connection.close()


async def run_single_worker_job(
    queue_key: str,
    *,
    worker_id: str,
    queue_slot: int,
    modal_function_call_id: str | None = None,
    post_success_hooks: PostSuccessHooks | None = None,
    harbor_variant_id: str | None = "default",
    execution_lane: str | None = "default",
    capacity_provider: str | None = None,
    capacity_slot: int | None = None,
    worker_billing_spec: WorkerBillingSpec | None = None,
) -> bool:
    """Claim and execute at most one `worker_jobs` row.

    Returns ``True`` if a row was claimed (regardless of the handler's
    outcome), ``False`` if the queue was empty. Exceptions from the
    handler are caught and reported through the outcome pipeline so the
    row never gets stuck in ``RUNNING``; only ``asyncio.CancelledError``
    propagates so Modal worker cancellation still unwinds cleanly.

    ``post_success_hooks`` fires after a SUCCESS has been durably
    recorded on the ``worker_jobs`` row. Hook exceptions are logged but
    do not fail the job -- they're operator notifications, not
    correctness-critical.
    """
    _ensure_handlers_registered()

    claim_kwargs: dict[str, Any] = {
        "worker_id": worker_id,
        "queue_slot": queue_slot,
        "modal_function_call_id": modal_function_call_id,
        "harbor_variant_id": harbor_variant_id,
    }
    if execution_lane != "default" or capacity_provider is not None:
        claim_kwargs.update(
            execution_lane=execution_lane,
            capacity_provider=capacity_provider,
            capacity_slot=capacity_slot,
        )
    job = await claim_single_worker_job(queue_key, **claim_kwargs)
    if job is None:
        return False

    attempt_started_at = job.claimed_at or datetime.now(timezone.utc)
    attempt_started_monotonic = time.monotonic()

    await open_worker_span(
        job,
        worker_billing_spec,
        started_at=attempt_started_at,
    )

    console.print(
        f"[cyan]Processing worker_job id={job.id} kind={job.kind.value} "
        f"(queue_key={queue_key}, attempt={job.attempts}/{job.max_attempts})[/cyan]"
    )

    try:
        handler = get_handler(job.kind)
    except NoHandlerRegisteredError as exc:
        # Fail the row instead of leaving it in RUNNING so cleanup
        # doesn't have to reap it via the stale-heartbeat sweep.
        outcome = JobOutcome.fail(
            f"No handler registered for kind={job.kind.value!r}: {exc}",
            retryable=False,
        )
    else:
        try:
            # Handlers receive the claimed projection; they can hydrate a
            # full ORM row if they need more columns.
            outcome = await handler.run(job)  # type: ignore[arg-type]
        except asyncio.CancelledError:
            console.print(f"[yellow]worker_job {job.id} cancelled[/yellow]")
            # This attempt's compute is over; close its worker span at cancel time
            # so the reconciler doesn't later close it at the job's (much later)
            # terminal finished_at. CAS close, so any other close path is a no-op.
            await close_worker_span(
                job.id, job.attempts, finished_at=datetime.now(timezone.utc)
            )
            raise
        except Exception as exc:  # handler-raised exceptions retry by default
            logger.exception(
                "worker_job %s (%s, subject=%s) handler error",
                job.id,
                job.kind.value,
                job.subject_id,
            )
            outcome = JobOutcome.fail(f"{type(exc).__name__}: {exc}", retryable=True)

    disposition_count = sum(
        value is not None
        for value in (outcome.success, outcome.failure, outcome.reroute)
    )
    if disposition_count != 1:
        # A handler can mutate the dataclass after construction. Keep an invalid
        # result from leaving its claimed worker_jobs row RUNNING indefinitely.
        outcome = JobOutcome.fail(
            "handler returned an invalid JobOutcome",
            retryable=False,
        )

    outcome_at = datetime.now(timezone.utc)
    persisted_status = await _record_outcome(
        job_id=job.id,
        worker_id=worker_id,
        outcome=outcome,
        attempts=job.attempts,
        max_attempts=job.max_attempts,
        kind=job.kind,
        subject_table=job.subject_table,
        subject_id=job.subject_id,
    )
    attempt_duration_seconds = time.monotonic() - attempt_started_monotonic
    outcome_recorded = bool(persisted_status)
    if isinstance(persisted_status, WorkerJobStatus):
        console.print(
            f"[dim]worker_job {job.id} -> {persisted_status.value} "
            f"(kind={job.kind.value}, queue_key={queue_key})[/dim]"
        )
        record_worker_job_transition(
            kind=job.kind,
            outcome=persisted_status,
            queue_key=job.queue_key,
            execution_lane=job.execution_lane,
            duration_seconds=attempt_duration_seconds,
        )

    if outcome_recorded:
        await close_worker_span(job.id, job.attempts, finished_at=outcome_at)

    if (
        outcome_recorded
        and outcome.success is not None
        and post_success_hooks
        and job.subject_id
    ):
        hook = post_success_hooks.get(job.kind)
        if hook is not None:
            try:
                await hook(job.subject_id)
            except Exception:
                logger.exception(
                    "post-success hook for kind=%s job=%s failed",
                    job.kind.value,
                    job.id,
                )

    return True


async def drain_worker_jobs(
    queue_key: str,
    *,
    worker_id: str,
    queue_slot: int,
    budget_seconds: float,
    modal_function_call_id: str | None = None,
    post_success_hooks: PostSuccessHooks | None = None,
    harbor_variant_id: str | None = "default",
    execution_lane: str | None = "default",
    capacity_provider: str | None = None,
    capacity_slot: int | None = None,
    worker_billing_spec: WorkerBillingSpec | None = None,
    _run_job: Callable[..., Awaitable[bool]] | None = None,
    _now: Callable[[], float] = time.monotonic,
) -> int:
    """Run ``worker_jobs`` back-to-back on one already-held queue slot.

    The worker model spawns one container per job, which then exits -- fine for
    long agent trials (minutes) but pathological for kinds whose jobs are
    shorter than the dispatcher poll interval (analysis ~54s, verdict ~9s,
    nop/oracle ~46s, vs a 180s poll): the job finishes seconds after spawn and
    the held slot then sits idle until the next poll, so those queues can never
    keep up no matter how high their concurrency limit.

    Draining keeps the container's slot busy: it claims and runs jobs for this
    ``queue_key`` until the queue drains or the wall-clock ``budget_seconds`` is
    spent. The budget auto-selects which kinds batch, with no per-kind config --
    a long job blows the budget on its first iteration and so still runs
    one-per-container, while short jobs pack many into one slot lease. The slot
    is acquired and released by the caller; this only reuses it across jobs and
    so must stay well under the slot lease window.

    Returns the number of jobs processed (0 if the queue was already empty).
    """
    run_job = _run_job or run_single_worker_job
    deadline = _now() + budget_seconds
    processed = 0
    while True:
        run_kwargs: dict[str, Any] = {
            "worker_id": worker_id,
            "queue_slot": queue_slot,
            "modal_function_call_id": modal_function_call_id,
            "post_success_hooks": post_success_hooks,
            "harbor_variant_id": harbor_variant_id,
            "worker_billing_spec": worker_billing_spec,
        }
        if execution_lane != "default" or capacity_provider is not None:
            run_kwargs.update(
                execution_lane=execution_lane,
                capacity_provider=capacity_provider,
                capacity_slot=capacity_slot,
            )
        job_found = await run_job(queue_key, **run_kwargs)
        if not job_found:
            break
        processed += 1
        if _now() >= deadline:
            break
    return processed

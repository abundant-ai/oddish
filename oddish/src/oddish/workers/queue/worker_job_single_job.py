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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import asyncpg

from oddish.config import settings
from oddish.db import WorkerJobKind, WorkerJobStatus
from oddish.workers.jobs.registry import (
    JobOutcome,
    NoHandlerRegisteredError,
    get_handler,
)
from oddish.workers.queue.shared import console


# Callback invoked after a claimed row completes successfully. Kept as a
# simple ``kind -> async fn(subject_id)`` dict so the backend can wire
# GitHub notifications (trial / analysis / verdict) without pushing
# backend-specific concerns into this module.
PostSuccessHooks = dict[WorkerJobKind, Callable[[str], Awaitable[None]]]


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
]


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
UPDATE worker_jobs
SET    status = 'RUNNING',
       claimed_at = NOW(),
       heartbeat_at = NOW(),
       attempts = attempts + 1,
       current_worker_id = $2,
       current_queue_slot = $3,
       modal_function_call_id = $4,
       -- started_at pins to the first attempt so "total elapsed
       -- across retries" is still recoverable. finished_at clears on
       -- re-claim so the duration query (finished_at - claimed_at)
       -- reflects only the last attempt.
       started_at = COALESCE(started_at, NOW()),
       finished_at = NULL,
       error_message = NULL
WHERE  id = (
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
          AND  tr2.deleted_at IS NULL
          AND  tk2.deleted_at IS NULL
        GROUP  BY COALESCE(tk2.created_by_user_id, tk2.user)
    ) rpg ON rpg.fairness_key = COALESCE(tk.created_by_user_id, tk.user)
    WHERE  wj.queue_key = $1
      AND  wj.status::text IN ('QUEUED', 'RETRYING')
      AND  wj.available_after <= NOW()
      -- Defense in depth: ``delete_*_core`` already cancels matching
      -- worker_jobs when a trial / task is soft-deleted, so this
      -- branch shouldn't trigger in practice. The guard is cheap and
      -- keeps the queue correct if a cancel ever races a claim. ``tr``
      -- and ``tk`` are populated only for TRIAL rows (via the LEFT
      -- JOINs above); for other kinds they are NULL and the
      -- ``IS NULL`` checks degenerate to TRUE.
      AND  (tr.deleted_at IS NULL)
      AND  (tk.deleted_at IS NULL)
    ORDER  BY wj.priority DESC,
              COALESCE(rpg.running_count, 0) ASC,
              wj.created_at ASC
    LIMIT  1
    FOR    UPDATE OF wj SKIP LOCKED
)
RETURNING id, kind::text AS kind, subject_table, subject_id, payload,
          attempts, max_attempts, queue_key, org_id, parent_job_id;
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
    worker_id: str | None = None
    queue_slot: int | None = None
    modal_function_call_id: str | None = None


async def _open_connection() -> asyncpg.Connection:
    return await asyncpg.connect(
        settings.asyncpg_url,
        statement_cache_size=0,
        server_settings=settings.asyncpg_server_settings(),
    )


async def heartbeat_worker_job(
    job_id: str,
    *,
    pending_failure_count: int = 0,
    pending_last_error: str | None = None,
) -> None:
    """Update a RUNNING worker_job's heartbeat timestamp.

    No-ops for terminal rows so a late heartbeat after SUCCESS / FAILED
    / CANCELLED can't resurrect a row. Follows the same failure-folding
    pattern as the trial heartbeat so a pooler blip produces a
    diagnostic breadcrumb rather than a silent stale-reap.
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
                """,
                job_id,
                pending_failure_count,
                (pending_last_error or "")[:500] or None,
            )
        else:
            await connection.execute(
                """
                UPDATE worker_jobs
                SET    heartbeat_at = NOW()
                WHERE  id = $1
                  AND  status::text = 'RUNNING'
                """,
                job_id,
            )
    finally:
        await connection.close()


async def claim_single_worker_job(
    queue_key: str,
    *,
    worker_id: str,
    queue_slot: int,
    modal_function_call_id: str | None = None,
) -> ClaimedWorkerJob | None:
    """Atomically claim at most one runnable ``worker_jobs`` row.

    Returns ``None`` if no row was available. The returned row is in
    ``RUNNING`` state with ``attempts`` incremented and claim metadata
    stamped.
    """
    connection = await _open_connection()
    try:
        row = await connection.fetchrow(
            _CLAIM_WORKER_JOB_SQL,
            queue_key,
            worker_id,
            queue_slot,
            modal_function_call_id,
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
        worker_id=worker_id,
        queue_slot=queue_slot,
        modal_function_call_id=modal_function_call_id,
    )


async def _record_outcome(
    *,
    job_id: str,
    outcome: JobOutcome,
    attempts: int,
    max_attempts: int,
) -> None:
    """Transition the claimed `worker_jobs` row to its terminal state.

    Success → status=SUCCESS, merge ``result_summary``, stamp
    ``finished_at``.
    Retryable failure with attempts remaining → status=RETRYING,
    stamp ``error_message``, clear claim metadata.
    Non-retryable (or retries exhausted) → status=FAILED.
    """
    connection = await _open_connection()
    try:
        if outcome.success is not None:
            import json

            summary = outcome.success.result_summary
            await connection.execute(
                """
                UPDATE worker_jobs
                SET    status = 'SUCCESS',
                       result_summary = $2::jsonb,
                       finished_at = NOW(),
                       heartbeat_at = NOW(),
                       error_message = NULL
                WHERE  id = $1
                """,
                job_id,
                json.dumps(summary) if summary is not None else None,
            )
            return

        assert outcome.failure is not None
        retry = outcome.failure.retryable and attempts < max_attempts
        if retry:
            # RETRYING is a scheduling state, not a terminal one. Leave
            # finished_at NULL so the claim SQL can clear it on the
            # next attempt without special-casing; the duration query
            # already filters to SUCCESS/FAILED so it doesn't observe
            # RETRYING rows either way.
            await connection.execute(
                """
                UPDATE worker_jobs
                SET    status = 'RETRYING',
                       error_message = $2,
                       current_worker_id = NULL,
                       current_queue_slot = NULL,
                       modal_function_call_id = NULL
                WHERE  id = $1
                """,
                job_id,
                outcome.failure.error_message,
            )
            console.print(
                f"metric=worker_job_retry_requeued id={job_id} "
                f"attempts={attempts}/{max_attempts}"
            )
        else:
            await connection.execute(
                """
                UPDATE worker_jobs
                SET    status = 'FAILED',
                       error_message = $2,
                       finished_at = NOW()
                WHERE  id = $1
                """,
                job_id,
                outcome.failure.error_message,
            )
    finally:
        await connection.close()


async def run_single_worker_job(
    queue_key: str,
    *,
    worker_id: str,
    queue_slot: int,
    modal_function_call_id: str | None = None,
    post_success_hooks: PostSuccessHooks | None = None,
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

    job = await claim_single_worker_job(
        queue_key,
        worker_id=worker_id,
        queue_slot=queue_slot,
        modal_function_call_id=modal_function_call_id,
    )
    if job is None:
        return False

    console.print(
        f"[cyan]Processing worker_job id={job.id} kind={job.kind.value} "
        f"(queue_key={queue_key}, attempt={job.attempts}/{job.max_attempts})[/cyan]"
    )

    try:
        handler = get_handler(job.kind)
    except NoHandlerRegisteredError as exc:
        # Fail the row instead of leaving it in RUNNING so cleanup
        # doesn't have to reap it via the stale-heartbeat sweep.
        await _record_outcome(
            job_id=job.id,
            outcome=JobOutcome.fail(
                f"No handler registered for kind={job.kind.value!r}: {exc}",
                retryable=False,
            ),
            attempts=job.attempts,
            max_attempts=job.max_attempts,
        )
        return True

    try:
        # Handlers receive the claimed projection; they can hydrate a
        # full ORM row if they need more columns.
        outcome = await handler.run(job)  # type: ignore[arg-type]
    except asyncio.CancelledError:
        console.print(f"[yellow]worker_job {job.id} cancelled[/yellow]")
        raise
    except Exception as exc:  # handler-raised exceptions are retryable by default
        console.print(f"[red]worker_job {job.id} handler error: {exc!r}[/red]")
        outcome = JobOutcome.fail(f"{type(exc).__name__}: {exc}", retryable=True)

    if (outcome.success is None) == (outcome.failure is None):
        # Defensive: the dataclass enforces this, but double-check so a
        # buggy handler can't leave a row RUNNING.
        outcome = JobOutcome.fail(
            "handler returned an invalid JobOutcome",
            retryable=False,
        )

    status = WorkerJobStatus.SUCCESS if outcome.success else WorkerJobStatus.FAILED
    console.print(
        f"[dim]worker_job {job.id} -> {status.value} "
        f"(kind={job.kind.value}, queue_key={queue_key})[/dim]"
    )

    await _record_outcome(
        job_id=job.id,
        outcome=outcome,
        attempts=job.attempts,
        max_attempts=job.max_attempts,
    )

    if outcome.success is not None and post_success_hooks and job.subject_id:
        hook = post_success_hooks.get(job.kind)
        if hook is not None:
            try:
                await hook(job.subject_id)
            except Exception as exc:
                console.print(
                    f"[yellow]post-success hook for kind={job.kind.value} "
                    f"job={job.id} failed: {exc}[/yellow]"
                )

    return True

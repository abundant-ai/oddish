from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import asyncpg
from pgqueuer.models import Job, TracebackRecord
from pgqueuer.qb import QueryQueueBuilder

from oddish.config import settings
from oddish.workers.queue.analysis_handler import run_analysis_job
from oddish.workers.queue.shared import console
from oddish.workers.queue.trial_handler import run_trial_job
from oddish.workers.queue.verdict_handler import run_verdict_job

_QUEUE_QB = QueryQueueBuilder()
_QUEUE_TABLE = _QUEUE_QB.settings.queue_table
_QUEUE_LOG_TABLE = _QUEUE_QB.settings.queue_table_log
_QUEUE_STATUS_TYPE = _QUEUE_QB.settings.queue_status_type

IdHook = Callable[[str], Awaitable[None]]

# ---------------------------------------------------------------------------
# Fair dequeue SQL
#
# Replaces PGQueuer's built-in FIFO dequeue with least-loaded-first
# scheduling so one user can't monopolize the queue.
#
# Strategy (within a single queue_key / entrypoint):
#   1. Stale picked jobs (past retry timer) get top priority
#   2. HIGH-priority jobs before LOW-priority jobs
#   3. Among same-priority jobs, prefer the *user* with fewer running trials
#   4. FIFO within a user
#
# Fairness key = COALESCE(tasks.created_by_user_id, tasks.user):
#   - Hosted: created_by_user_id (Clerk user ID) distinguishes individuals
#   - OSS:    tasks.user (submission label) is the fallback
#   Users within the same org get fair shares; users across orgs do too.
# ---------------------------------------------------------------------------
_FAIR_DEQUEUE_SQL = f"""
WITH updated AS (
    UPDATE {_QUEUE_TABLE}
    SET status = 'picked',
        updated = NOW(),
        heartbeat = NOW(),
        queue_manager_id = $2
    WHERE id = (
        SELECT q.id
        FROM {_QUEUE_TABLE} q
        LEFT JOIN trials t ON t.id = (
            CASE WHEN q.payload IS NOT NULL
                 THEN convert_from(q.payload, 'UTF8')::jsonb ->> 'trial_id'
            END
        )
        LEFT JOIN tasks tk ON tk.id = t.task_id
        LEFT JOIN (
            SELECT COALESCE(tk2.created_by_user_id, tk2.user) AS fairness_key,
                   COUNT(*) AS running_count
            FROM trials tr
            JOIN tasks tk2 ON tk2.id = tr.task_id
            WHERE tr.status = 'running' AND tr.queue_key = $1
            GROUP BY COALESCE(tk2.created_by_user_id, tk2.user)
        ) rpg ON rpg.fairness_key = COALESCE(tk.created_by_user_id, tk.user)
        WHERE q.entrypoint = $1
          AND q.execute_after < NOW()
          AND (
              q.status = 'queued'
              OR (q.status = 'picked'
                  AND $3 > interval '0'
                  AND q.heartbeat < NOW() - $3)
          )
        ORDER BY
            CASE WHEN q.status = 'picked' THEN 0 ELSE 1 END,
            q.priority DESC,
            COALESCE(rpg.running_count, 0) ASC,
            q.id ASC
        LIMIT 1
        FOR UPDATE OF q SKIP LOCKED
    )
    RETURNING *
),
_log AS (
    INSERT INTO {_QUEUE_LOG_TABLE} (job_id, status, entrypoint, priority)
    SELECT id, status, entrypoint, priority FROM updated
)
SELECT * FROM updated;
"""


def _job_owner(job: Job) -> UUID:
    if job.queue_manager_id is None:
        raise RuntimeError(f"Claimed job {job.id} missing queue_manager_id")
    return job.queue_manager_id


async def _open_connection() -> asyncpg.Connection:
    return await asyncpg.connect(
        settings.asyncpg_url,
        statement_cache_size=0,
    )


async def claim_single_job(queue_key: str) -> Job | None:
    """Claim at most one job using fair round-robin scheduling.

    Uses least-loaded-first ordering: the job chosen belongs to the org/task
    group with the fewest currently running trials in this queue.  Within a
    group, priority then FIFO ordering is preserved.  Stale picked jobs (past
    the retry timer) are always prioritized over new queued jobs.
    """
    retry_timer = timedelta(minutes=settings.trial_retry_timer_minutes)
    connection = await _open_connection()
    try:
        qmid = uuid4()
        row = await connection.fetchrow(
            _FAIR_DEQUEUE_SQL,
            queue_key,
            qmid,
            retry_timer,
        )
        if row is None:
            return None
        job = Job.model_validate(dict(row))
        _job_owner(job)
        return job
    finally:
        await connection.close()


async def _update_job_heartbeat(job: Job) -> bool:
    connection = await _open_connection()
    try:
        result = await connection.execute(
            f"""
            UPDATE {_QUEUE_TABLE}
            SET heartbeat = NOW()
            WHERE id = $1
              AND queue_manager_id = $2
              AND status = 'picked'
            """,
            int(job.id),
            _job_owner(job),
        )
    finally:
        await connection.close()

    try:
        return int(str(result).split()[-1]) > 0
    except Exception:
        return False


async def _heartbeat_claimed_job(*, job: Job, stop_event: asyncio.Event) -> None:
    heartbeat_interval_seconds = max(
        1.0,
        timedelta(minutes=settings.trial_retry_timer_minutes).total_seconds() / 2,
    )
    while True:
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=heartbeat_interval_seconds,
            )
        except TimeoutError:
            pass

        if stop_event.is_set():
            return

        try:
            updated = await _update_job_heartbeat(job)
            if not updated:
                console.print(
                    f"[yellow]Lost ownership while heartbeating job {job.id}; stopping pgqueuer heartbeat[/yellow]"
                )
                return
        except Exception as exc:
            console.print(f"[yellow]PGQueuer heartbeat update failed: {exc}[/yellow]")


async def _finalize_claimed_job(
    *,
    job: Job,
    status: str,
    traceback_record: TracebackRecord | None = None,
) -> bool:
    """Delete and log a picked job only if this worker still owns it."""
    connection = await _open_connection()
    try:
        row = await connection.fetchrow(
            f"""
            WITH deleted AS (
                DELETE FROM {_QUEUE_TABLE}
                WHERE id = $1
                  AND queue_manager_id = $2
                RETURNING id, entrypoint, priority
            )
            INSERT INTO {_QUEUE_LOG_TABLE} (
                job_id,
                status,
                entrypoint,
                priority,
                traceback
            )
            SELECT
                deleted.id,
                $3::{_QUEUE_STATUS_TYPE},
                deleted.entrypoint,
                deleted.priority,
                $4::jsonb
            FROM deleted
            RETURNING job_id
            """,
            int(job.id),
            _job_owner(job),
            status,
            traceback_record.model_dump_json() if traceback_record else None,
        )
    finally:
        await connection.close()
    return row is not None


def _cancellation_traceback_record(
    *,
    job: Job,
    worker_id: str,
    queue_slot: int,
    exc: asyncio.CancelledError,
) -> TracebackRecord:
    return TracebackRecord(
        job_id=job.id,
        timestamp=datetime.now(timezone.utc),
        exception_type=exc.__class__.__name__,
        exception_message=str(exc) or "Worker task was cancelled",
        traceback="",
        additional_context={
            "entrypoint": job.entrypoint,
            "queue_manager_id": str(_job_owner(job)),
            "worker_id": worker_id,
            "queue_slot": queue_slot,
        },
    )


async def _run_hook(hook: IdHook | None, value: str) -> None:
    if hook is not None:
        await hook(value)


async def _dispatch_claimed_job(
    *,
    job: Job,
    queue_key: str,
    worker_id: str,
    queue_slot: int,
    modal_function_call_id: str | None = None,
    prepare_trial: IdHook | None = None,
    on_trial_complete: IdHook | None = None,
    on_analysis_complete: IdHook | None = None,
    on_verdict_complete: IdHook | None = None,
) -> None:
    if job.payload is None:
        raise ValueError(f"Job {job.id} has empty payload")

    raw = job.payload.decode(errors="replace")
    try:
        payload = json.loads(raw)
    except Exception as e:
        console.print(
            f"[red]Invalid job payload JSON (queue_key={queue_key}, job_id={job.id}): {e}[/red]"
        )
        console.print(f"[dim]Raw payload: {raw!r}[/dim]")
        raise

    job_type = payload.get("job_type")
    if job_type is None and payload.get("trial_id"):
        job_type = "trial"
        console.print(
            f"[yellow]Job missing job_type; treating as trial (queue_key={queue_key}, job_id={job.id})[/yellow]"
        )

    console.print(
        f"[cyan]Processing job_type={job_type} job_id={job.id} (queue_key={queue_key})[/cyan]"
    )

    if job_type == "trial":
        if not payload.get("trial_id"):
            raise ValueError(
                f"Trial job missing trial_id (queue_key={queue_key}, job_id={job.id})"
            )
        trial_id = payload["trial_id"]
        await _run_hook(prepare_trial, trial_id)
        await run_trial_job(
            job,
            queue_key=queue_key,
            worker_id=worker_id,
            queue_slot=queue_slot,
            modal_function_call_id=modal_function_call_id,
        )
        await _run_hook(on_trial_complete, trial_id)
        return

    if job_type == "analysis":
        if not payload.get("trial_id"):
            raise ValueError(
                f"Analysis job missing trial_id (queue_key={queue_key}, job_id={job.id})"
            )
        trial_id = payload["trial_id"]
        await run_analysis_job(job, queue_key=queue_key)
        await _run_hook(on_analysis_complete, trial_id)
        return

    if job_type == "verdict":
        if not payload.get("task_id"):
            raise ValueError(
                f"Verdict job missing task_id (queue_key={queue_key}, job_id={job.id})"
            )
        task_id = payload["task_id"]
        await run_verdict_job(job, queue_key=queue_key)
        await _run_hook(on_verdict_complete, task_id)
        return

    raise ValueError(
        f"Unknown job_type={job_type!r} (queue_key={queue_key}, job_id={job.id})"
    )


async def run_single_job(
    queue_key: str,
    *,
    worker_id: str,
    queue_slot: int,
    modal_function_call_id: str | None = None,
    prepare_trial: IdHook | None = None,
    on_trial_complete: IdHook | None = None,
    on_analysis_complete: IdHook | None = None,
    on_verdict_complete: IdHook | None = None,
) -> bool:
    """Claim, execute, and finalize at most one job with short-lived DB connections."""
    job = await claim_single_job(queue_key)
    if job is None:
        return False

    stop_event = asyncio.Event()
    heartbeat_task = asyncio.create_task(
        _heartbeat_claimed_job(job=job, stop_event=stop_event)
    )

    try:
        try:
            await _dispatch_claimed_job(
                job=job,
                queue_key=queue_key,
                worker_id=worker_id,
                queue_slot=queue_slot,
                modal_function_call_id=modal_function_call_id,
                prepare_trial=prepare_trial,
                on_trial_complete=on_trial_complete,
                on_analysis_complete=on_analysis_complete,
                on_verdict_complete=on_verdict_complete,
            )
        except asyncio.CancelledError as exc:
            finalized = await _finalize_claimed_job(
                job=job,
                status="canceled",
                traceback_record=_cancellation_traceback_record(
                    job=job,
                    worker_id=worker_id,
                    queue_slot=queue_slot,
                    exc=exc,
                ),
            )
            if not finalized:
                console.print(
                    f"[yellow]Skipped cancellation finalization for job {job.id}; worker no longer owns it[/yellow]"
                )
            raise
        except Exception as exc:
            console.print(f"[red]Job {job.id} failed: {exc}[/red]")
            traceback_record = TracebackRecord.from_exception(
                exc=exc,
                job_id=job.id,
                additional_context={
                    "entrypoint": job.entrypoint,
                    "queue_manager_id": str(_job_owner(job)),
                    "worker_id": worker_id,
                    "queue_slot": queue_slot,
                },
            )
            finalized = await _finalize_claimed_job(
                job=job,
                status="exception",
                traceback_record=traceback_record,
            )
            if not finalized:
                console.print(
                    f"[yellow]Skipped exception finalization for job {job.id}; worker no longer owns it[/yellow]"
                )
            return True

        finalized = await _finalize_claimed_job(job=job, status="successful")
        if not finalized:
            console.print(
                f"[yellow]Skipped successful finalization for job {job.id}; worker no longer owns it[/yellow]"
            )
        return True
    finally:
        stop_event.set()
        await asyncio.gather(heartbeat_task, return_exceptions=True)

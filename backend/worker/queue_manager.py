import asyncio
import json
from datetime import timedelta
from uuid import UUID, uuid4

import asyncpg
from pgqueuer.models import Job, TracebackRecord
from pgqueuer.qb import QueryQueueBuilder
from pgqueuer.queries import EntrypointExecutionParameter, Queries

from cloud_policy import enforce_trial_environment
from oddish.config import settings
from oddish.workers.queue import run_analysis_job, run_trial_job, run_verdict_job

from .github import notify_github_analysis, notify_github_trial, notify_github_verdict
from .runtime import console

_QUEUE_QB = QueryQueueBuilder()
_QUEUE_TABLE = _QUEUE_QB.settings.queue_table
_QUEUE_LOG_TABLE = _QUEUE_QB.settings.queue_table_log
_QUEUE_STATUS_TYPE = _QUEUE_QB.settings.queue_status_type


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
    """Claim at most one job without keeping the DB connection open."""
    retry_timer = timedelta(minutes=settings.trial_retry_timer_minutes)
    connection = await _open_connection()
    try:
        queries = Queries.from_asyncpg_connection(connection)
        jobs = await queries.dequeue(
            batch_size=1,
            entrypoints={
                queue_key: EntrypointExecutionParameter(
                    retry_after=retry_timer,
                    serialized=False,
                    concurrency_limit=1,
                )
            },
            queue_manager_id=uuid4(),
            global_concurrency_limit=1,
        )
        if not jobs:
            return None
        job = jobs[0]
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


async def _dispatch_claimed_job(
    *,
    job: Job,
    queue_key: str,
    worker_id: str,
    queue_slot: int,
    modal_function_call_id: str | None = None,
) -> None:
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
        await enforce_trial_environment(trial_id)
        await run_trial_job(
            job,
            queue_key=queue_key,
            worker_id=worker_id,
            queue_slot=queue_slot,
            modal_function_call_id=modal_function_call_id,
        )
        await notify_github_trial(trial_id)
        return

    if job_type == "analysis":
        if not payload.get("trial_id"):
            raise ValueError(
                f"Analysis job missing trial_id (queue_key={queue_key}, job_id={job.id})"
            )
        trial_id = payload["trial_id"]
        await run_analysis_job(job, queue_key=queue_key)
        await notify_github_analysis(trial_id)
        return

    if job_type == "verdict":
        if not payload.get("task_id"):
            raise ValueError(
                f"Verdict job missing task_id (queue_key={queue_key}, job_id={job.id})"
            )
        task_id = payload["task_id"]
        await run_verdict_job(job, queue_key=queue_key)
        await notify_github_verdict(task_id)
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
            )
        except asyncio.CancelledError:
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

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import asyncpg

from oddish.config import settings
from oddish.workers.queue.analysis_handler import run_analysis_job
from oddish.workers.queue.shared import console
from oddish.workers.queue.trial_handler import run_trial_job
from oddish.workers.queue.verdict_handler import run_verdict_job

IdHook = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class ClaimedJob:
    """Lightweight representation of a claimed queue item."""

    job_type: str  # "trial", "analysis", "verdict"
    trial_id: str | None = None
    task_id: str | None = None
    queue_key: str = ""


# ---------------------------------------------------------------------------
# Fair trial claim SQL
#
# Uses least-loaded-first scheduling so one user can't monopolize the queue.
#
# Strategy (within a single queue_key):
#   1. HIGH-priority tasks before LOW-priority tasks
#   2. Among same-priority, prefer the user with fewer running trials
#   3. FIFO within a user
#
# Fairness key = COALESCE(tasks.created_by_user_id, tasks.user):
#   - Hosted: created_by_user_id (Clerk user ID) distinguishes individuals
#   - OSS:    tasks.user (submission label) is the fallback
# ---------------------------------------------------------------------------
_CLAIM_TRIAL_SQL = """
UPDATE trials
SET status = 'RUNNING',
    claimed_at = NOW(),
    heartbeat_at = NOW()
WHERE id = (
    SELECT t.id
    FROM trials t
    JOIN tasks tk ON tk.id = t.task_id
    LEFT JOIN (
        SELECT COALESCE(tk2.created_by_user_id, tk2.user) AS fairness_key,
               COUNT(*) AS running_count
        FROM trials tr
        JOIN tasks tk2 ON tk2.id = tr.task_id
        WHERE tr.status::text = 'RUNNING' AND tr.queue_key = $1
        GROUP BY COALESCE(tk2.created_by_user_id, tk2.user)
    ) rpg ON rpg.fairness_key = COALESCE(tk.created_by_user_id, tk.user)
    WHERE t.queue_key = $1
      AND t.status::text IN ('QUEUED', 'RETRYING')
    ORDER BY
        CASE WHEN tk.priority::text = 'HIGH' THEN 0 ELSE 1 END,
        COALESCE(rpg.running_count, 0) ASC,
        t.created_at ASC
    LIMIT 1
    FOR UPDATE OF t SKIP LOCKED
)
RETURNING id, task_id;
"""

_CLAIM_ANALYSIS_SQL = """
UPDATE trials
SET analysis_status = 'RUNNING',
    analysis_started_at = NOW()
WHERE id = (
    SELECT t.id
    FROM trials t
    WHERE t.analysis_status::text = 'QUEUED'
      AND t.status::text IN ('SUCCESS', 'FAILED')
    ORDER BY t.created_at ASC
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
RETURNING id, task_id;
"""

_CLAIM_VERDICT_SQL = """
UPDATE tasks
SET verdict_status = 'RUNNING',
    verdict_started_at = NOW()
WHERE id = (
    SELECT t.id
    FROM tasks t
    WHERE t.verdict_status::text = 'QUEUED'
      AND t.status::text = 'VERDICT_PENDING'
    ORDER BY t.created_at ASC
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
RETURNING id;
"""


async def _open_connection() -> asyncpg.Connection:
    return await asyncpg.connect(
        settings.asyncpg_url,
        statement_cache_size=0,
    )


async def claim_single_job(queue_key: str) -> ClaimedJob | None:
    """Claim at most one job using fair scheduling.

    Routes to the appropriate claim query based on queue_key:
    - Analysis queue key -> claim from trials.analysis_status
    - Verdict queue key  -> claim from tasks.verdict_status
    - Everything else    -> claim trial with fair user-level scheduling
    """
    analysis_key = settings.normalize_queue_key(settings.get_analysis_queue_key())
    verdict_key = settings.normalize_queue_key(settings.get_verdict_queue_key())
    normalized_key = settings.normalize_queue_key(queue_key)

    connection = await _open_connection()
    try:
        if normalized_key == analysis_key:
            row = await connection.fetchrow(_CLAIM_ANALYSIS_SQL)
            if row is None:
                return None
            return ClaimedJob(
                job_type="analysis",
                trial_id=str(row["id"]),
                task_id=str(row["task_id"]),
                queue_key=queue_key,
            )

        if normalized_key == verdict_key:
            row = await connection.fetchrow(_CLAIM_VERDICT_SQL)
            if row is None:
                return None
            return ClaimedJob(
                job_type="verdict",
                task_id=str(row["id"]),
                queue_key=queue_key,
            )

        row = await connection.fetchrow(_CLAIM_TRIAL_SQL, queue_key)
        if row is None:
            return None
        return ClaimedJob(
            job_type="trial",
            trial_id=str(row["id"]),
            task_id=str(row["task_id"]),
            queue_key=queue_key,
        )
    finally:
        await connection.close()


async def _run_hook(hook: IdHook | None, value: str) -> None:
    if hook is not None:
        await hook(value)


async def _dispatch_claimed_job(
    *,
    job: ClaimedJob,
    queue_key: str,
    worker_id: str,
    queue_slot: int,
    modal_function_call_id: str | None = None,
    prepare_trial: IdHook | None = None,
    on_trial_complete: IdHook | None = None,
    on_analysis_complete: IdHook | None = None,
    on_verdict_complete: IdHook | None = None,
) -> None:
    console.print(
        f"[cyan]Processing job_type={job.job_type} (queue_key={queue_key})[/cyan]"
    )

    if job.job_type == "trial":
        if not job.trial_id:
            raise ValueError("Trial job missing trial_id")
        await _run_hook(prepare_trial, job.trial_id)
        await run_trial_job(
            job.trial_id,
            queue_key=queue_key,
            worker_id=worker_id,
            queue_slot=queue_slot,
            modal_function_call_id=modal_function_call_id,
        )
        await _run_hook(on_trial_complete, job.trial_id)
        return

    if job.job_type == "analysis":
        if not job.trial_id:
            raise ValueError("Analysis job missing trial_id")
        await run_analysis_job(job.trial_id, queue_key=queue_key)
        await _run_hook(on_analysis_complete, job.trial_id)
        return

    if job.job_type == "verdict":
        if not job.task_id:
            raise ValueError("Verdict job missing task_id")
        await run_verdict_job(job.task_id, queue_key=queue_key)
        await _run_hook(on_verdict_complete, job.task_id)
        return

    raise ValueError(f"Unknown job_type={job.job_type!r}")


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
    """Claim and execute at most one job.

    The claim atomically sets the trial/task status to RUNNING via
    FOR UPDATE SKIP LOCKED.  Handlers manage their own error state;
    stale-heartbeat cleanup handles crashes.
    """
    job = await claim_single_job(queue_key)
    if job is None:
        return False

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
    except asyncio.CancelledError:
        console.print(
            f"[yellow]Job {job.job_type} cancelled "
            f"(trial={job.trial_id}, task={job.task_id})[/yellow]"
        )
        raise
    except Exception as exc:
        console.print(
            f"[red]Job {job.job_type} failed "
            f"(trial={job.trial_id}, task={job.task_id}): {exc}[/red]"
        )

    return True

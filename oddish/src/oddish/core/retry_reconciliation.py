"""Repair trial mirrors only when their execution jobs are already terminal.

Never infer successful execution, grant more attempts, or recreate work here.
A missing job or a SUCCESS job without settled trial evidence needs inspection.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.task_browse_summary import refresh_task_browse_summaries


async def reconcile_terminal_retry_trials(
    session: AsyncSession,
    *,
    org_id: str | None = None,
    trial_ids: list[str] | None = None,
    stale_after_minutes: int = 15,
    limit: int = 250,
    dry_run: bool = True,
) -> list[dict]:
    """Return before/after records; apply in the caller's transaction if asked.

    Lock Task → Trial → WorkerJob, matching manual retry/cancel. SKIP LOCKED
    leaves concurrent edits for the next sweep. The active-job exclusion looks
    at *all* jobs for the subject, not merely the most recent terminal attempt.
    """
    locking = "" if dry_run else "FOR UPDATE OF t, tr, j SKIP LOCKED"
    rows = (
        (
            await session.execute(
                text("""
                SELECT tr.id, tr.task_id, tr.task_version_id, tr.status::text AS old_status,
                       tr.attempts AS old_attempts, tr.error_message AS old_error,
                       tr.superseded_by_trial_id, j.id AS job_id,
                       j.status::text AS job_status, j.attempts AS job_attempts,
                       j.max_attempts AS job_max_attempts,
                       j.finished_at AS job_finished_at, j.error_message AS job_error
                FROM tasks t
                JOIN trials tr ON tr.task_id = t.id
                JOIN worker_jobs j ON j.id = (
                    SELECT latest.id FROM worker_jobs latest
                    WHERE latest.subject_table = 'trials' AND latest.subject_id = tr.id
                      AND latest.kind::text = 'TRIAL'
                    ORDER BY latest.created_at DESC, latest.id DESC LIMIT 1
                )
                WHERE tr.status = 'RETRYING'
                  AND tr.deleted_at IS NULL AND t.deleted_at IS NULL
                  AND (CAST(:org_id AS TEXT) IS NULL OR tr.org_id = :org_id)
                  AND (CAST(:trial_ids AS TEXT[]) IS NULL OR tr.id = ANY(CAST(:trial_ids AS TEXT[])))
                  AND tr.updated_at < NOW() - make_interval(mins => :stale_minutes)
                  AND j.status::text IN ('FAILED', 'CANCELLED')
                  AND j.finished_at < NOW() - make_interval(mins => :stale_minutes)
                  AND NOT EXISTS (
                      SELECT 1 FROM worker_jobs active
                      WHERE active.subject_table = 'trials' AND active.subject_id = tr.id
                        AND active.kind::text = 'TRIAL'
                        AND active.status::text IN ('QUEUED', 'RUNNING', 'RETRYING', 'BLOCKED')
                  )
                ORDER BY t.id, tr.id
                LIMIT :limit
                """ + locking),
                {
                    "org_id": org_id,
                    "trial_ids": trial_ids,
                    "stale_minutes": stale_after_minutes,
                    "limit": limit,
                },
            )
        )
        .mappings()
        .all()
    )
    changes = [dict(row, new_status="FAILED") for row in rows]
    if dry_run or not changes:
        return changes

    for row in changes:
        await session.execute(
            text("""
                UPDATE trials
                SET status = 'FAILED', attempts = GREATEST(attempts, :attempts),
                    error_message = COALESCE(:error, error_message),
                    harbor_stage = CASE WHEN :cancelled THEN 'cancelled' ELSE harbor_stage END,
                    finished_at = :finished_at, next_retry_at = NULL,
                    current_worker_id = NULL, current_queue_slot = NULL,
                    updated_at = NOW()
                WHERE id = :trial_id AND status = 'RETRYING'
            """),
            {
                "trial_id": row["id"],
                "attempts": row["job_attempts"],
                "error": row["job_error"],
                "cancelled": row["job_status"] == "CANCELLED",
                "finished_at": row["job_finished_at"],
            },
        )
    await refresh_task_browse_summaries(
        session, [row["task_version_id"] for row in changes]
    )
    return changes

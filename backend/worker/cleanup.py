from sqlalchemy import select, text

from oddish.db import (
    AnalysisStatus,
    TaskModel,
    TrialModel,
    TrialStatus,
    get_session,
    utcnow,
)
from oddish.queue import cancel_pgqueuer_jobs_for_trials, maybe_start_analysis_stage

ORPHANED_STATE_STALE_AFTER_MINUTES = 10


def orphan_cleanup_error(
    issue: str,
    *,
    stale_after_minutes: int,
    existing_error: str | None,
) -> str:
    if issue == "queued_without_job":
        reason = (
            "Trial was cancelled by queue cleanup because it stayed queued without a "
            f"backing pgqueuer job for over {stale_after_minutes} minutes."
        )
    elif issue == "retrying_without_job":
        reason = (
            "Trial retry was cancelled by queue cleanup because it had no backing "
            f"pgqueuer job for over {stale_after_minutes} minutes."
        )
    elif issue == "running_without_picked_job":
        reason = (
            "Trial was cancelled by queue cleanup because its worker lost the picked "
            "pgqueuer job that should have been driving execution."
        )
    elif issue == "running_stale_heartbeat":
        reason = (
            "Trial was cancelled by queue cleanup because the worker heartbeat went "
            f"stale for over {stale_after_minutes} minutes."
        )
    else:
        reason = "Trial was cancelled by queue cleanup due to orphaned runtime state."

    existing = (existing_error or "").strip()
    if not existing:
        return reason
    if existing.startswith(reason):
        return existing
    return f"{reason}\n\nPrevious error: {existing}"


async def cleanup_orphaned_queue_state(
    *,
    stale_after_minutes: int = ORPHANED_STATE_STALE_AFTER_MINUTES,
) -> dict[str, int]:
    """Cancel stale/orphaned trial state so the queue can make forward progress."""
    issue_rows: list[tuple[str, str]] = []
    terminal_trial_ids_with_jobs: list[str] = []

    async with get_session() as session:
        issue_rows = list(
            (
                await session.execute(
                    text(
                        """
                        WITH picked_jobs AS (
                            SELECT id
                            FROM pgqueuer
                            WHERE status = 'picked'
                        ),
                        active_trial_jobs AS (
                            SELECT convert_from(payload, 'utf8')::jsonb->>'trial_id' AS trial_id
                            FROM pgqueuer
                            WHERE status IN ('queued', 'picked')
                        )
                        SELECT trial_id, issue
                        FROM (
                            SELECT
                                t.id AS trial_id,
                                CASE
                                    WHEN t.status::text = 'QUEUED'
                                         AND COALESCE(t.updated_at, t.created_at) <
                                             NOW() - make_interval(mins => :stale_after_minutes)
                                         AND NOT EXISTS (
                                             SELECT 1
                                             FROM active_trial_jobs j
                                             WHERE j.trial_id = t.id
                                         )
                                        THEN 'queued_without_job'
                                    WHEN t.status::text = 'RETRYING'
                                         AND COALESCE(t.updated_at, t.heartbeat_at, t.claimed_at, t.created_at) <
                                             NOW() - make_interval(mins => :stale_after_minutes)
                                         AND NOT EXISTS (
                                             SELECT 1
                                             FROM active_trial_jobs j
                                             WHERE j.trial_id = t.id
                                         )
                                        THEN 'retrying_without_job'
                                    WHEN t.status::text = 'RUNNING'
                                         AND COALESCE(t.heartbeat_at, t.claimed_at, t.updated_at, t.created_at) <
                                             NOW() - make_interval(mins => :stale_after_minutes)
                                         AND (
                                             t.current_pgqueuer_job_id IS NULL
                                             OR NOT EXISTS (
                                                 SELECT 1
                                                 FROM picked_jobs p
                                                 WHERE p.id = t.current_pgqueuer_job_id
                                             )
                                         )
                                        THEN 'running_without_picked_job'
                                    WHEN t.status::text = 'RUNNING'
                                         AND (
                                             t.heartbeat_at IS NULL
                                             OR t.heartbeat_at <
                                                NOW() - make_interval(mins => :stale_after_minutes)
                                         )
                                        THEN 'running_stale_heartbeat'
                                    ELSE NULL
                                END AS issue
                            FROM trials t
                        ) issues
                        WHERE issue IS NOT NULL
                        ORDER BY trial_id
                        """
                    ),
                    {"stale_after_minutes": stale_after_minutes},
                )
            ).all()
        )

        terminal_trial_ids_with_jobs = [
            row[0]
            for row in (
                await session.execute(
                    text(
                        """
                        SELECT t.id
                        FROM trials t
                        WHERE t.status::text IN ('SUCCESS', 'FAILED')
                          AND EXISTS (
                              SELECT 1
                              FROM pgqueuer p
                              WHERE convert_from(p.payload, 'utf8')::jsonb->>'trial_id' = t.id
                                AND p.status IN ('queued', 'picked')
                          )
                        """
                    )
                )
            ).all()
        ]

        trial_ids_to_update = [trial_id for trial_id, _ in issue_rows]
        trial_ids_to_cancel_jobs = set(trial_ids_to_update) | set(terminal_trial_ids_with_jobs)
        if trial_ids_to_cancel_jobs:
            await cancel_pgqueuer_jobs_for_trials(session, list(trial_ids_to_cancel_jobs))

        if trial_ids_to_update:
            issue_by_trial_id = {trial_id: issue for trial_id, issue in issue_rows}
            trials = (
                await session.execute(
                    select(TrialModel).where(TrialModel.id.in_(trial_ids_to_update))
                )
            ).scalars().all()

            for trial in trials:
                issue = issue_by_trial_id[trial.id]
                task = await session.get(TaskModel, trial.task_id)
                error_message = orphan_cleanup_error(
                    issue,
                    stale_after_minutes=stale_after_minutes,
                    existing_error=trial.error_message,
                )
                trial.status = TrialStatus.FAILED
                trial.error_message = error_message
                trial.finished_at = trial.finished_at or utcnow()
                trial.current_pgqueuer_job_id = None
                trial.current_worker_id = None
                trial.current_queue_slot = None
                trial.heartbeat_at = utcnow()
                if trial.harbor_stage not in {"completed", "cancelled"}:
                    trial.harbor_stage = "cancelled"

                if (
                    task
                    and task.run_analysis
                    and trial.analysis_status not in (AnalysisStatus.SUCCESS, AnalysisStatus.FAILED)
                ):
                    trial.analysis_status = AnalysisStatus.FAILED
                    trial.analysis_error = (
                        "Analysis skipped because the trial was cancelled during "
                        "orphaned queue cleanup."
                    )
                    trial.analysis_finished_at = utcnow()

            await session.flush()

            for trial_id in trial_ids_to_update:
                await maybe_start_analysis_stage(session, trial_id)

    counts = {
        "queued_without_job": 0,
        "retrying_without_job": 0,
        "running_without_picked_job": 0,
        "running_stale_heartbeat": 0,
        "terminal_jobs_cancelled": len(terminal_trial_ids_with_jobs),
    }
    for _, issue in issue_rows:
        counts[issue] = counts.get(issue, 0) + 1
    return counts

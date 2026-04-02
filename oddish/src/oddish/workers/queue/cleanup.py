from sqlalchemy import select, text

from oddish.config import settings
from oddish.db import (
    AnalysisStatus,
    Priority,
    TaskModel,
    TaskStatus,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    get_session,
    utcnow,
)
from oddish.queue import (
    cancel_pgqueuer_job_ids,
    cancel_pgqueuer_jobs_for_trials,
    enqueue_analysis,
    enqueue_trial,
    enqueue_verdict,
    maybe_start_analysis_stage,
    maybe_start_verdict_stage,
)

ORPHANED_STATE_STALE_AFTER_MINUTES = 10
PICKED_ANALYSIS_STALE_AFTER_MINUTES = 30
PICKED_VERDICT_STALE_AFTER_MINUTES = 15


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


def _clear_trial_runtime_refs(trial: TrialModel) -> None:
    trial.current_pgqueuer_job_id = None
    trial.current_worker_id = None
    trial.current_queue_slot = None
    trial.modal_function_call_id = None


def _should_requeue_analysis(trial: TrialModel) -> bool:
    return trial.analysis_status not in (AnalysisStatus.SUCCESS, AnalysisStatus.FAILED)


def _should_requeue_verdict(task: TaskModel) -> bool:
    return (
        task.status != TaskStatus.COMPLETED
        and task.verdict_status not in (VerdictStatus.SUCCESS, VerdictStatus.FAILED)
    )


async def cleanup_orphaned_queue_state(
    *,
    stale_after_minutes: int = ORPHANED_STATE_STALE_AFTER_MINUTES,
) -> dict[str, int]:
    """Reconcile stale queue/runtime state so the queue can make forward progress."""
    issue_rows: list[tuple[str, str]] = []
    terminal_trial_job_ids: list[int] = []
    orphaned_picked_job_ids: list[int] = []
    analysis_trial_ids_to_requeue: list[str] = []
    stale_picked_analysis_rows: list[tuple[int, str]] = []
    stale_picked_verdict_rows: list[tuple[int, str]] = []
    tasks_ready_for_analysis: list[str] = []
    tasks_ready_for_verdict: list[str] = []
    stale_verdict_task_ids: list[str] = []

    queued_without_job_reenqueued = 0
    retrying_without_job_reenqueued = 0
    analysis_queued_without_job_reenqueued = 0
    running_jobs_cancelled = 0
    terminal_jobs_cancelled = 0
    orphaned_picked_jobs_cancelled = 0
    stale_picked_analysis_jobs_cancelled = 0
    stale_picked_analysis_jobs_reenqueued = 0
    stale_picked_verdict_jobs_cancelled = 0
    stale_picked_verdict_jobs_reenqueued = 0
    tasks_progressed_to_analysis = 0
    tasks_progressed_to_verdict = 0
    stale_verdict_tasks_reenqueued = 0
    stale_verdict_tasks_completed = 0
    terminal_trial_runtime_refs_cleared = 0
    orphaned_active_slots_cleared = 0

    async with get_session() as session:
        issue_rows = [
            (str(r[0]), str(r[1]))
            for r in (
                await session.execute(
                    text(
                        """
                        WITH picked_trial_jobs AS (
                            SELECT id
                            FROM pgqueuer
                            WHERE status = 'picked'
                              AND payload IS NOT NULL
                              AND convert_from(payload, 'utf8')::jsonb ? 'trial_id'
                              AND COALESCE(
                                  convert_from(payload, 'utf8')::jsonb->>'job_type',
                                  'trial'
                              ) = 'trial'
                        ),
                        active_trial_jobs AS (
                            SELECT convert_from(payload, 'utf8')::jsonb->>'trial_id' AS trial_id
                            FROM pgqueuer
                            WHERE status IN ('queued', 'picked')
                              AND payload IS NOT NULL
                              AND convert_from(payload, 'utf8')::jsonb ? 'trial_id'
                              AND COALESCE(
                                  convert_from(payload, 'utf8')::jsonb->>'job_type',
                                  'trial'
                              ) = 'trial'
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
                                                 FROM picked_trial_jobs p
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
            if r[0] is not None and r[1] is not None
        ]

        terminal_trial_job_ids = [
            int(row[0])
            for row in (
                await session.execute(
                    text(
                        """
                        SELECT p.id
                        FROM pgqueuer p
                        JOIN trials t
                          ON convert_from(p.payload, 'utf8')::jsonb->>'trial_id' = t.id
                        WHERE p.status IN ('queued', 'picked')
                          AND p.payload IS NOT NULL
                          AND convert_from(p.payload, 'utf8')::jsonb ? 'trial_id'
                          AND COALESCE(
                              convert_from(p.payload, 'utf8')::jsonb->>'job_type',
                              'trial'
                          ) = 'trial'
                          AND t.status::text IN ('SUCCESS', 'FAILED')
                        """
                    )
                )
            ).all()
            if row[0] is not None
        ]

        orphaned_picked_job_ids = [
            int(row[0])
            for row in (
                await session.execute(
                    text(
                        """
                        SELECT DISTINCT p.id AS job_id
                        FROM pgqueuer p
                        LEFT JOIN trials t
                          ON t.id::text = convert_from(p.payload, 'utf8')::jsonb->>'trial_id'
                        WHERE p.status = 'picked'
                          AND p.payload IS NOT NULL
                          AND convert_from(p.payload, 'utf8')::jsonb ? 'trial_id'
                          AND COALESCE(
                              convert_from(p.payload, 'utf8')::jsonb->>'job_type',
                              'trial'
                          ) = 'trial'
                          AND COALESCE(p.heartbeat, p.updated, p.created) <
                              NOW() - make_interval(mins => :stale_after_minutes)
                          AND (
                              t.id IS NULL
                              OR t.status::text NOT IN ('RUNNING', 'RETRYING')
                              OR t.current_pgqueuer_job_id IS DISTINCT FROM p.id
                          )
                        """
                    ),
                    {"stale_after_minutes": stale_after_minutes},
                )
            ).all()
            if row[0] is not None
        ]

        analysis_trial_ids_to_requeue = [
            str(row[0])
            for row in (
                await session.execute(
                    text(
                        """
                        WITH active_analysis_jobs AS (
                            SELECT convert_from(payload, 'utf8')::jsonb->>'trial_id' AS trial_id
                            FROM pgqueuer
                            WHERE status IN ('queued', 'picked')
                              AND payload IS NOT NULL
                              AND convert_from(payload, 'utf8')::jsonb->>'job_type' = 'analysis'
                        )
                        SELECT t.id
                        FROM trials t
                        WHERE t.analysis_status::text = 'QUEUED'
                          AND COALESCE(
                              t.updated_at,
                              t.analysis_finished_at,
                              t.analysis_started_at,
                              t.created_at
                          ) < NOW() - make_interval(mins => :stale_after_minutes)
                          AND NOT EXISTS (
                              SELECT 1
                              FROM active_analysis_jobs a
                              WHERE a.trial_id = t.id
                          )
                        ORDER BY t.id
                        """
                    ),
                    {"stale_after_minutes": stale_after_minutes},
                )
            ).all()
            if row[0] is not None
        ]

        stale_picked_analysis_rows = [
            (int(row[0]), str(row[1]))
            for row in (
                await session.execute(
                    text(
                        """
                        SELECT p.id AS job_id, t.id AS trial_id
                        FROM pgqueuer p
                        JOIN trials t
                          ON t.id = convert_from(p.payload, 'utf8')::jsonb->>'trial_id'
                        WHERE p.status = 'picked'
                          AND p.payload IS NOT NULL
                          AND convert_from(p.payload, 'utf8')::jsonb->>'job_type' = 'analysis'
                          AND COALESCE(p.heartbeat, p.updated, p.created) <
                              NOW() - make_interval(mins => :stale_after_minutes)
                        ORDER BY p.id
                        """
                    ),
                    {"stale_after_minutes": PICKED_ANALYSIS_STALE_AFTER_MINUTES},
                )
            ).all()
            if row[0] is not None and row[1] is not None
        ]

        stale_picked_verdict_rows = [
            (int(row[0]), str(row[1]))
            for row in (
                await session.execute(
                    text(
                        """
                        SELECT p.id AS job_id, t.id AS task_id
                        FROM pgqueuer p
                        JOIN tasks t
                          ON t.id = convert_from(p.payload, 'utf8')::jsonb->>'task_id'
                        WHERE p.status = 'picked'
                          AND p.payload IS NOT NULL
                          AND convert_from(p.payload, 'utf8')::jsonb->>'job_type' = 'verdict'
                          AND COALESCE(p.heartbeat, p.updated, p.created) <
                              NOW() - make_interval(mins => :stale_after_minutes)
                        ORDER BY p.id
                        """
                    ),
                    {"stale_after_minutes": PICKED_VERDICT_STALE_AFTER_MINUTES},
                )
            ).all()
            if row[0] is not None and row[1] is not None
        ]

        tasks_ready_for_analysis = [
            str(row[0])
            for row in (
                await session.execute(
                    text(
                        """
                        SELECT MIN(tr.id) AS trial_id
                        FROM tasks t
                        JOIN trials tr ON tr.task_id = t.id
                        WHERE t.status = 'RUNNING'
                        GROUP BY t.id
                        HAVING COUNT(*) FILTER (
                            WHERE tr.status IN ('PENDING', 'QUEUED', 'RUNNING', 'RETRYING')
                        ) = 0
                        """
                    )
                )
            ).all()
            if row[0] is not None
        ]

        tasks_ready_for_verdict = [
            str(row[0])
            for row in (
                await session.execute(
                    text(
                        """
                        SELECT MIN(tr.id) AS trial_id
                        FROM tasks t
                        JOIN trials tr ON tr.task_id = t.id
                        WHERE t.status = 'ANALYZING'
                        GROUP BY t.id
                        HAVING COUNT(*) FILTER (
                            WHERE tr.analysis_status IS NULL
                               OR tr.analysis_status IN ('PENDING', 'QUEUED', 'RUNNING')
                        ) = 0
                        """
                    )
                )
            ).all()
            if row[0] is not None
        ]

        stale_verdict_task_ids = [
            str(row[0])
            for row in (
                await session.execute(
                    text(
                        """
                        SELECT t.id
                        FROM tasks t
                        WHERE t.status = 'VERDICT_PENDING'
                          AND NOT EXISTS (
                              SELECT 1
                              FROM pgqueuer p
                              WHERE p.status IN ('queued', 'picked')
                                AND p.payload IS NOT NULL
                                AND convert_from(p.payload, 'utf8')::jsonb->>'task_id' = t.id
                                AND convert_from(p.payload, 'utf8')::jsonb->>'job_type' = 'verdict'
                          )
                        ORDER BY t.id
                        """
                    )
                )
            ).all()
            if row[0] is not None
        ]

        issue_by_trial_id = {trial_id: issue for trial_id, issue in issue_rows}
        trial_ids_to_requeue = [
            trial_id
            for trial_id, issue in issue_rows
            if issue in {"queued_without_job", "retrying_without_job"}
        ]
        trial_ids_to_fail = [
            trial_id
            for trial_id, issue in issue_rows
            if issue in {"running_without_picked_job", "running_stale_heartbeat"}
        ]

        if terminal_trial_job_ids:
            terminal_jobs_cancelled = await cancel_pgqueuer_job_ids(
                session,
                terminal_trial_job_ids,
                suppress_errors=False,
            )

        if trial_ids_to_fail:
            running_jobs_cancelled = await cancel_pgqueuer_jobs_for_trials(
                session,
                trial_ids_to_fail,
                suppress_errors=False,
            )

        if orphaned_picked_job_ids:
            orphaned_picked_jobs_cancelled = await cancel_pgqueuer_job_ids(
                session,
                orphaned_picked_job_ids,
                suppress_errors=False,
            )

        stale_picked_analysis_job_ids = sorted(
            {job_id for job_id, _ in stale_picked_analysis_rows}
        )
        if stale_picked_analysis_job_ids:
            stale_picked_analysis_jobs_cancelled = await cancel_pgqueuer_job_ids(
                session,
                stale_picked_analysis_job_ids,
                suppress_errors=False,
            )

        stale_picked_verdict_job_ids = sorted(
            {job_id for job_id, _ in stale_picked_verdict_rows}
        )
        if stale_picked_verdict_job_ids:
            stale_picked_verdict_jobs_cancelled = await cancel_pgqueuer_job_ids(
                session,
                stale_picked_verdict_job_ids,
                suppress_errors=False,
            )

        all_trial_ids = sorted(
            {
                *trial_ids_to_requeue,
                *trial_ids_to_fail,
                *analysis_trial_ids_to_requeue,
                *(trial_id for _, trial_id in stale_picked_analysis_rows),
            }
        )
        all_task_ids = sorted(
            {
                *stale_verdict_task_ids,
                *(task_id for _, task_id in stale_picked_verdict_rows),
            }
        )

        trials_by_id = {
            trial.id: trial
            for trial in (
                (
                    await session.execute(
                        select(TrialModel).where(TrialModel.id.in_(all_trial_ids))
                    )
                )
                .scalars()
                .all()
            )
        }
        tasks_by_id = {
            task.id: task
            for task in (
                (
                    await session.execute(
                        select(TaskModel).where(TaskModel.id.in_(all_task_ids))
                    )
                )
                .scalars()
                .all()
            )
        }

        for trial_id in trial_ids_to_requeue:
            trial = trials_by_id.get(trial_id)
            if not trial:
                continue
            issue = issue_by_trial_id[trial.id]
            task = await session.get(TaskModel, trial.task_id)
            model = settings.normalize_trial_model(trial.agent, trial.model)
            queue_key = trial.queue_key or settings.get_queue_key_for_trial(
                trial.agent,
                model,
            )
            if trial.queue_key != queue_key:
                trial.queue_key = queue_key
            _clear_trial_runtime_refs(trial)
            trial.claimed_at = None
            trial.heartbeat_at = None
            pgq_priority = 1000 if task and task.priority == Priority.HIGH else 0
            await enqueue_trial(
                session,
                trial.id,
                queue_key,
                priority=pgq_priority,
            )
            if issue == "queued_without_job":
                queued_without_job_reenqueued += 1
            else:
                retrying_without_job_reenqueued += 1

        for trial_id in analysis_trial_ids_to_requeue:
            trial = trials_by_id.get(trial_id)
            if not trial or trial.analysis_status != AnalysisStatus.QUEUED:
                continue
            await enqueue_analysis(
                session,
                trial.id,
                queue_key=settings.get_analysis_queue_key(),
            )
            analysis_queued_without_job_reenqueued += 1

        for _, trial_id in stale_picked_analysis_rows:
            trial = trials_by_id.get(trial_id)
            if not trial or not _should_requeue_analysis(trial):
                continue
            trial.analysis_status = AnalysisStatus.QUEUED
            trial.analysis_error = None
            trial.analysis_started_at = None
            trial.analysis_finished_at = None
            await enqueue_analysis(
                session,
                trial.id,
                queue_key=settings.get_analysis_queue_key(),
            )
            stale_picked_analysis_jobs_reenqueued += 1

        for trial_id in trial_ids_to_fail:
            trial = trials_by_id.get(trial_id)
            if not trial:
                continue
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
            _clear_trial_runtime_refs(trial)
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

        for _, task_id in stale_picked_verdict_rows:
            task = tasks_by_id.get(task_id)
            if not task or not _should_requeue_verdict(task):
                continue
            task.status = TaskStatus.VERDICT_PENDING
            task.verdict_status = VerdictStatus.QUEUED
            task.verdict_error = None
            task.verdict_started_at = None
            task.verdict_finished_at = None
            await enqueue_verdict(
                session,
                task.id,
                queue_key=settings.get_verdict_queue_key(),
            )
            stale_picked_verdict_jobs_reenqueued += 1

        for trial_id in trial_ids_to_fail:
            if await maybe_start_analysis_stage(session, trial_id):
                tasks_progressed_to_analysis += 1

        for trial_id in tasks_ready_for_analysis:
            if await maybe_start_analysis_stage(session, trial_id):
                tasks_progressed_to_analysis += 1

        for trial_id in tasks_ready_for_verdict:
            if await maybe_start_verdict_stage(session, trial_id):
                tasks_progressed_to_verdict += 1

        for task_id in stale_verdict_task_ids:
            task = tasks_by_id.get(task_id)
            if not task or task.status != TaskStatus.VERDICT_PENDING:
                continue
            if task.verdict_status in (VerdictStatus.SUCCESS, VerdictStatus.FAILED):
                task.status = TaskStatus.COMPLETED
                task.finished_at = task.finished_at or utcnow()
                stale_verdict_tasks_completed += 1
                continue
            task.verdict_status = VerdictStatus.QUEUED
            task.verdict_error = None
            task.verdict_started_at = None
            task.verdict_finished_at = None
            await enqueue_verdict(
                session,
                task.id,
                queue_key=settings.get_verdict_queue_key(),
            )
            stale_verdict_tasks_reenqueued += 1

        terminal_trial_runtime_refs_cleared = int(
            (
                await session.execute(
                    text(
                        """
                        UPDATE trials
                        SET current_pgqueuer_job_id = NULL,
                            current_worker_id = NULL,
                            current_queue_slot = NULL,
                            modal_function_call_id = NULL
                        WHERE status::text IN ('SUCCESS', 'FAILED')
                          AND (
                              current_pgqueuer_job_id IS NOT NULL
                              OR current_worker_id IS NOT NULL
                              OR current_queue_slot IS NOT NULL
                              OR modal_function_call_id IS NOT NULL
                          )
                        """
                    )
                )
            ).rowcount
            or 0
        )

        orphaned_active_slots_cleared = int(
            (
                await session.execute(
                    text(
                        """
                        UPDATE queue_slots qs
                        SET locked_by = NULL,
                            locked_until = NULL
                        WHERE qs.locked_by IS NOT NULL
                          AND qs.locked_until IS NOT NULL
                          AND qs.locked_until > NOW()
                          AND NOT EXISTS (
                              SELECT 1
                              FROM pgqueuer p
                              WHERE p.status = 'picked'
                                AND p.entrypoint = qs.queue_key
                          )
                        """
                    )
                )
            ).rowcount
            or 0
        )

    counts = {
        "queued_without_job": 0,
        "retrying_without_job": 0,
        "analysis_queued_without_job_reenqueued": analysis_queued_without_job_reenqueued,
        "running_without_picked_job": 0,
        "running_stale_heartbeat": 0,
        "queued_without_job_reenqueued": queued_without_job_reenqueued,
        "retrying_without_job_reenqueued": retrying_without_job_reenqueued,
        "running_jobs_cancelled": running_jobs_cancelled,
        "terminal_jobs_cancelled": terminal_jobs_cancelled,
        "orphaned_picked_jobs_cancelled": orphaned_picked_jobs_cancelled,
        "stale_picked_analysis_jobs_cancelled": stale_picked_analysis_jobs_cancelled,
        "stale_picked_analysis_jobs_reenqueued": stale_picked_analysis_jobs_reenqueued,
        "stale_picked_verdict_jobs_cancelled": stale_picked_verdict_jobs_cancelled,
        "stale_picked_verdict_jobs_reenqueued": stale_picked_verdict_jobs_reenqueued,
        "tasks_progressed_to_analysis": tasks_progressed_to_analysis,
        "tasks_progressed_to_verdict": tasks_progressed_to_verdict,
        "stale_verdict_tasks_reenqueued": stale_verdict_tasks_reenqueued,
        "stale_verdict_tasks_completed": stale_verdict_tasks_completed,
        "terminal_trial_runtime_refs_cleared": terminal_trial_runtime_refs_cleared,
        "orphaned_active_slots_cleared": orphaned_active_slots_cleared,
    }
    for _, issue in issue_rows:
        counts[issue] = counts.get(issue, 0) + 1
    return counts

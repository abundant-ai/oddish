"""Unified cleanup sweep for the `worker_jobs` queue.

Before the unified refactor this module had five separate steps, one
per domain table flavor (running-trials, stale-analysis,
stale-verdict, stage-transition, orphaned-slots). They all collapse
into two kind-agnostic passes now:

1. **Zombie 'idle in transaction' reaper**. Unchanged; runs first so
   its ``AccessShareLock``s don't block the UPDATEs below. Safe to
   run on every dispatcher tick.
2. **Stale-heartbeat sweep on worker_jobs**. One query transitions
   every RUNNING row whose heartbeat stalled into RETRYING (if
   retries remain) or FAILED. Per-kind domain-row cleanup is driven
   off the returned rows.

The stage-transition helpers (``maybe_start_analysis_stage`` /
``maybe_start_verdict_stage``) still run as a safety net so tasks
with all trials done can't get stuck if a single stage-transition
flush failed at handler-commit time.
"""

from typing import cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult

from oddish.config import settings
from oddish.db import (
    AnalysisStatus,
    TaskModel,
    TaskStatus,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    get_session,
    utcnow,
)
from oddish.workers.queue.shared import console

# See historical context: we bumped this from 10 -> 15 after a
# pooler-blip incident reaped 25-70 healthy trials in a single sweep.
# 15 minutes is forgiving enough to ride out transient pooler pressure
# without meaningfully delaying detection of actually-crashed workers.
STALE_HEARTBEAT_MINUTES = 15

# Age at which an "idle in transaction" backend is considered a zombie
# from a SIGKILLed worker. Must stay above the server-side
# idle_in_transaction_session_timeout so we never fight Postgres's own
# enforcement; this reaper only catches deployments where that GUC is
# ignored (older Supavisor, etc).
ZOMBIE_IDLE_MINUTES = 10


async def reap_idle_in_transaction_zombies(
    *,
    idle_after_minutes: int = ZOMBIE_IDLE_MINUTES,
) -> int:
    """Terminate Postgres backends stuck 'idle in transaction' for too long.

    Motivated by real incidents: when a Modal worker is SIGKILLed by the
    cancel API mid-transaction, the TCP connection to the pooler dies
    but the Postgres backend keeps holding row/table locks -- sometimes
    for hours. In one observed incident a single bulk cancel left 26
    such zombies holding AccessShareLock on `trials` for 1h43m,
    blocking every subsequent heartbeat write and DDL migration.

    Targeting: only sessions whose `application_name` is in the
    configured reaper allow-list (so we never match Supabase-internal
    services like postgrest / pg_cron / Supabase Storage API Canary).
    """
    allowed_names = [n for n in (settings.db_reaper_application_names or []) if n]
    if not allowed_names:
        return 0

    try:
        async with get_session() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT pid, pg_terminate_backend(pid) AS terminated
                        FROM pg_stat_activity
                        WHERE state = 'idle in transaction'
                          AND application_name = ANY(:app_names)
                          AND state_change < NOW() - make_interval(mins => :idle_after_minutes)
                          AND pid <> pg_backend_pid()
                        """
                    ),
                    {
                        "app_names": allowed_names,
                        "idle_after_minutes": idle_after_minutes,
                    },
                )
            ).all()
    except Exception as exc:
        # pg_terminate_backend requires privileges we may not have in
        # every deployment. Don't let that fail the whole sweep --
        # zombie reaping is a safety net, not a correctness requirement.
        console.print(f"[yellow]Zombie transaction reaper skipped: {exc}[/yellow]")
        return 0

    terminated = sum(1 for row in rows if row.terminated)
    if terminated > 0:
        console.print(
            f"metric=zombie_txn_reaped count={terminated} "
            f"idle_after_minutes={idle_after_minutes}"
        )
        console.print(
            f"[yellow]Reaped {terminated} zombie 'idle in transaction' "
            f"backend(s) (application_names={allowed_names}, "
            f"idle>{idle_after_minutes}m)[/yellow]"
        )
    return terminated


async def cleanup_orphaned_queue_state(
    *,
    stale_after_minutes: int = STALE_HEARTBEAT_MINUTES,
) -> dict[str, int]:
    """Reconcile stale scheduling state so the queue can make progress.

    The only scheduling failure mode after the unified refactor is a
    ``worker_jobs`` row stuck in ``RUNNING`` with a stale heartbeat
    (worker crashed without committing its terminal state). Everything
    else -- stage transitions, terminal-runtime-ref cleanup -- is
    either handled by the handler commit or kept as a safety net here.
    """
    worker_jobs_retried = 0
    worker_jobs_failed = 0
    tasks_progressed_to_analysis = 0
    tasks_progressed_to_verdict = 0
    terminal_trial_runtime_refs_cleared = 0
    orphaned_active_slots_cleared = 0
    verdict_pending_completed = 0

    zombie_txn_reaped = await reap_idle_in_transaction_zombies()

    # Lazy import: ``oddish.queue`` imports ``oddish.workers.jobs.enqueue``
    # which transitively imports this module, so a top-level import
    # would race with module initialization.
    from oddish.queue import maybe_start_analysis_stage, maybe_start_verdict_stage

    async with get_session() as session:
        # -----------------------------------------------------------------
        # 1. Stale-heartbeat sweep on worker_jobs.
        #    Transitions RUNNING rows whose heartbeat stalled to
        #    RETRYING (attempts remain) or FAILED (exhausted). This
        #    is the single place stale-reap retry policy lives --
        #    compare with three per-table queries in the legacy
        #    cleanup.
        # -----------------------------------------------------------------
        stale_rows = (
            (
                await session.execute(
                    text(
                        """
                    UPDATE worker_jobs
                    SET    status = CASE
                               WHEN attempts < max_attempts THEN 'RETRYING'::worker_job_status
                               ELSE 'FAILED'::worker_job_status
                           END,
                           stale_reaped_at = NOW(),
                           finished_at = CASE
                               WHEN attempts < max_attempts THEN finished_at
                               ELSE NOW()
                           END,
                           current_worker_id = NULL,
                           current_queue_slot = NULL,
                           modal_function_call_id = NULL,
                           error_message = CASE
                               WHEN heartbeat_failure_count > 0 AND last_heartbeat_error IS NOT NULL
                                   THEN 'Worker heartbeat stalled for over '
                                        || :stale_after_minutes
                                        || ' minutes. Worker reported '
                                        || heartbeat_failure_count
                                        || ' write failures; last error: '
                                        || last_heartbeat_error
                               ELSE 'Worker heartbeat stalled for over '
                                    || :stale_after_minutes
                                    || ' minutes.'
                           END
                    WHERE  status::text = 'RUNNING'
                      AND  (
                          heartbeat_at IS NULL
                          OR heartbeat_at < NOW() - make_interval(mins => :stale_after_minutes)
                      )
                    RETURNING id,
                              kind::text AS kind,
                              status::text AS new_status,
                              subject_table,
                              subject_id,
                              attempts,
                              max_attempts,
                              error_message
                    """
                    ),
                    {"stale_after_minutes": stale_after_minutes},
                )
            )
            .mappings()
            .all()
        )

        # Mirror the terminal worker_jobs state back onto the domain
        # rows (``trials`` / ``tasks``) so dashboards don't lag. This
        # is the per-kind piece of the cleanup -- but it's bounded to
        # the stale rows we just reaped, so the cost is O(stale) not
        # O(table).
        stale_trial_ids: list[str] = []
        for row in stale_rows:
            if row["new_status"] == "RETRYING":
                worker_jobs_retried += 1
            else:
                worker_jobs_failed += 1

            kind = row["kind"]
            subject_id = row["subject_id"]
            if not subject_id:
                continue

            if kind == "TRIAL":
                trial = await session.get(TrialModel, str(subject_id))
                if trial is None:
                    continue
                if row["new_status"] == "RETRYING":
                    # Domain row goes back to RETRYING so the UI
                    # reflects "waiting for another attempt". The new
                    # worker_jobs claim will bump trials.status back
                    # to RUNNING via ``_prepare_trial_run``.
                    trial.status = TrialStatus.RETRYING
                    trial.error_message = row["error_message"]
                    trial.current_worker_id = None
                    trial.current_queue_slot = None
                    trial.stale_reaped_at = utcnow()
                else:
                    trial.status = TrialStatus.FAILED
                    trial.error_message = row["error_message"]
                    trial.finished_at = trial.finished_at or utcnow()
                    trial.current_worker_id = None
                    trial.current_queue_slot = None
                    trial.stale_reaped_at = utcnow()
                    if trial.harbor_stage not in {"completed", "cancelled"}:
                        trial.harbor_stage = "cancelled"

                    task = await session.get(TaskModel, trial.task_id)
                    if (
                        task
                        and task.run_analysis
                        and trial.analysis_status
                        not in (AnalysisStatus.SUCCESS, AnalysisStatus.FAILED)
                    ):
                        trial.analysis_status = AnalysisStatus.FAILED
                        trial.analysis_error = (
                            "Analysis skipped because the trial was "
                            "cancelled during orphaned queue cleanup."
                        )
                        trial.analysis_finished_at = utcnow()
                    stale_trial_ids.append(trial.id)

            elif kind == "ANALYSIS":
                trial = await session.get(TrialModel, str(subject_id))
                if trial is None:
                    continue
                if row["new_status"] == "FAILED":
                    trial.analysis_status = AnalysisStatus.FAILED
                    trial.analysis_error = row["error_message"]
                    trial.analysis_finished_at = utcnow()
                else:
                    # Retrying: show "queued for retry" in the UI rather
                    # than leaving the row on RUNNING. The handler
                    # resets to QUEUED explicitly on next claim as well.
                    trial.analysis_status = AnalysisStatus.QUEUED
                    trial.analysis_error = row["error_message"]

            elif kind == "VERDICT":
                task = await session.get(TaskModel, str(subject_id))
                if task is None:
                    continue
                if row["new_status"] == "FAILED":
                    task.verdict_status = VerdictStatus.FAILED
                    task.verdict_error = row["error_message"]
                    task.verdict_finished_at = utcnow()
                else:
                    task.verdict_status = VerdictStatus.QUEUED
                    task.verdict_error = row["error_message"]

        await session.flush()

        # Trigger stage transitions for tasks whose trials just got
        # failed, in case the failure marks the task "all trials done"
        # for the first time.
        for trial_id in stale_trial_ids:
            if await maybe_start_analysis_stage(session, trial_id):
                tasks_progressed_to_analysis += 1

        # -----------------------------------------------------------------
        # 2. Tasks stuck in RUNNING where all trials finished -> advance.
        #    Safety net in case a handler's ``maybe_start_analysis_stage``
        #    call didn't run (e.g. the handler was killed between
        #    writing the trial terminal state and committing the stage
        #    transition).
        # -----------------------------------------------------------------
        tasks_ready_for_analysis = (
            await session.execute(
                text(
                    """
                    SELECT MIN(tr.id) AS trial_id
                    FROM tasks t
                    JOIN trials tr ON tr.task_id = t.id
                    WHERE t.status = 'RUNNING'
                    GROUP BY t.id
                    HAVING COUNT(*) FILTER (
                        WHERE tr.status IN ('QUEUED', 'RUNNING', 'RETRYING')
                    ) = 0
                    """
                )
            )
        ).all()

        for (trial_id,) in tasks_ready_for_analysis:
            if trial_id and await maybe_start_analysis_stage(session, str(trial_id)):
                tasks_progressed_to_analysis += 1

        # -----------------------------------------------------------------
        # 3. Tasks stuck in ANALYZING where all analyses finished -> advance.
        # -----------------------------------------------------------------
        tasks_ready_for_verdict = (
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
                           OR tr.analysis_status IN ('QUEUED', 'RUNNING')
                    ) = 0
                    """
                )
            )
        ).all()

        for (trial_id,) in tasks_ready_for_verdict:
            if trial_id and await maybe_start_verdict_stage(session, str(trial_id)):
                tasks_progressed_to_verdict += 1

        # -----------------------------------------------------------------
        # 4. VERDICT_PENDING tasks with no queued/running verdict_status.
        #    Either their worker_jobs VERDICT row finished and we never
        #    saw the hook, or the task was created before the unified
        #    refactor and has no verdict row at all -- re-enqueue it so
        #    the dispatcher has something to claim.
        # -----------------------------------------------------------------
        stale_verdict_pending = (
            await session.execute(
                text(
                    """
                    SELECT id
                    FROM tasks
                    WHERE status = 'VERDICT_PENDING'
                      AND (
                          verdict_status IS NULL
                          OR verdict_status::text NOT IN ('QUEUED', 'RUNNING')
                      )
                    """
                )
            )
        ).all()

        from oddish.queue import enqueue_verdict_worker_job

        for (task_id,) in stale_verdict_pending:
            task = await session.get(TaskModel, str(task_id))
            if not task or task.status != TaskStatus.VERDICT_PENDING:
                continue
            if task.verdict_status in (VerdictStatus.SUCCESS, VerdictStatus.FAILED):
                task.status = TaskStatus.COMPLETED
                task.finished_at = task.finished_at or utcnow()
                verdict_pending_completed += 1
            else:
                task.verdict_status = VerdictStatus.QUEUED
                task.verdict_error = None
                task.verdict_started_at = None
                task.verdict_finished_at = None
                await enqueue_verdict_worker_job(
                    session, task_id=task.id, org_id=task.org_id
                )

        # -----------------------------------------------------------------
        # 5. Clear stale claim metadata on terminal trials (pure
        #    display-layer hygiene; scheduling state already lives on
        #    worker_jobs).
        # -----------------------------------------------------------------
        terminal_trial_cleanup_result = cast(
            CursorResult,
            await session.execute(
                text(
                    """
                    UPDATE trials
                    SET    current_worker_id = NULL,
                           current_queue_slot = NULL
                    WHERE  status::text IN ('SUCCESS', 'FAILED')
                      AND  (
                          current_worker_id IS NOT NULL
                          OR current_queue_slot IS NOT NULL
                      )
                    """
                )
            ),
        )
        terminal_trial_runtime_refs_cleared = int(
            terminal_trial_cleanup_result.rowcount or 0
        )

        # -----------------------------------------------------------------
        # 6. Release queue slot leases whose worker_jobs row is no
        #    longer RUNNING on that key.
        # -----------------------------------------------------------------
        orphaned_slot_cleanup_result = cast(
            CursorResult,
            await session.execute(
                text(
                    """
                    UPDATE queue_slots qs
                    SET    locked_by = NULL,
                           locked_until = NULL
                    WHERE  qs.locked_by IS NOT NULL
                      AND  qs.locked_until IS NOT NULL
                      AND  qs.locked_until > NOW()
                      AND  NOT EXISTS (
                          SELECT 1
                          FROM   worker_jobs wj
                          WHERE  wj.status::text = 'RUNNING'
                            AND  wj.queue_key = qs.queue_key
                      )
                    """
                )
            ),
        )
        orphaned_active_slots_cleared = int(orphaned_slot_cleanup_result.rowcount or 0)

    return {
        "worker_jobs_retried": worker_jobs_retried,
        "worker_jobs_failed": worker_jobs_failed,
        "tasks_progressed_to_analysis": tasks_progressed_to_analysis,
        "tasks_progressed_to_verdict": tasks_progressed_to_verdict,
        "verdict_pending_completed": verdict_pending_completed,
        "terminal_trial_runtime_refs_cleared": terminal_trial_runtime_refs_cleared,
        "orphaned_active_slots_cleared": orphaned_active_slots_cleared,
        "zombie_txn_reaped": zombie_txn_reaped,
    }

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
from oddish.queue import maybe_start_analysis_stage, maybe_start_verdict_stage
from oddish.workers.queue.shared import console

# Bumped from 10 -> 15 after incidents where a burst of user-cancels /
# Supabase pooler pressure caused the whole worker fleet's heartbeat writes to
# stall for ~12-17 minutes, which then reaped 25-70 healthy in-flight trials
# in a single sweep. 15 minutes is more forgiving of transient pooler blips
# without meaningfully delaying detection of actually-crashed workers.
STALE_HEARTBEAT_MINUTES = 15

# Age at which an "idle in transaction" backend is considered a zombie from a
# SIGKILLed worker (typical legitimate transactions are <5s). Must be greater
# than the idle_in_transaction_session_timeout server setting so we never
# fight Postgres's built-in enforcement; this only catches cases where that
# enforcement was disabled or the session GUC didn't take effect (e.g.
# older Supavisor versions that don't pass through startup parameters).
ZOMBIE_IDLE_MINUTES = 10


def _clear_trial_runtime_refs(trial: TrialModel) -> None:
    trial.current_worker_id = None
    trial.current_queue_slot = None
    trial.modal_function_call_id = None


def _clear_analysis_runtime_refs(trial: TrialModel) -> None:
    trial.analysis_modal_function_call_id = None


def _clear_verdict_runtime_refs(task: TaskModel) -> None:
    task.verdict_modal_function_call_id = None


async def reap_idle_in_transaction_zombies(
    *,
    idle_after_minutes: int = ZOMBIE_IDLE_MINUTES,
) -> int:
    """Terminate Postgres backends stuck "idle in transaction" for too long.

    Motivated by real incidents: when a Modal worker is SIGKILLed by the
    cancel API (`terminate_containers=True`) mid-transaction, the TCP
    connection to the pooler dies but the Postgres backend on the other
    side of the pooler keeps holding the row/table locks the transaction
    had acquired -- potentially forever. In one observed incident a single
    bulk cancel left 26 such zombies holding `AccessShareLock` on `trials`
    for 1h43m, blocking every subsequent heartbeat write and DDL migration.

    Returns the number of backends terminated. Safe to call on every
    dispatcher tick; this does nothing when there are no zombies.

    Targeting: only sessions whose `application_name` is in the configured
    reaper allow-list. On Supabase this includes the pooler identity
    ('Supavisor') -- Supabase-internal services use distinct names
    ('postgrest', 'pg_cron scheduler', 'Supabase Storage API Canary',
    etc) and are never matched.
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
        # every deployment (self-hosted Postgres, tests, etc). Don't let
        # that fail the whole cleanup sweep -- zombie reaping is a
        # safety net, not a correctness requirement.
        console.print(
            f"[yellow]Zombie transaction reaper skipped: {exc}[/yellow]"
        )
        return 0

    terminated = sum(1 for row in rows if row.terminated)
    if terminated > 0:
        console.print(
            f"metric=zombie_txn_reaped count={terminated} idle_after_minutes={idle_after_minutes}"
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
    """Reconcile stale queue/runtime state so the queue can make forward progress.

    With the trials table serving as the queue directly, the only failure modes
    are:
      - A trial is RUNNING but the worker crashed (stale heartbeat)
      - A task is stuck in an intermediate pipeline stage
      - Terminal trials still hold runtime refs
    """
    running_stale_heartbeat_failed = 0
    stale_analysis_reset = 0
    stale_verdict_reset = 0
    tasks_progressed_to_analysis = 0
    tasks_progressed_to_verdict = 0
    terminal_trial_runtime_refs_cleared = 0
    orphaned_active_slots_cleared = 0

    # Reap zombie 'idle in transaction' backends BEFORE everything else:
    # they hold AccessShareLocks on trials that block the UPDATEs further
    # down in this function. Runs in its own session so a permissions
    # error can't abort the rest of the cleanup sweep.
    zombie_txn_reaped = await reap_idle_in_transaction_zombies()

    async with get_session() as session:
        # ---------------------------------------------------------------
        # 1. Running trials with stale heartbeats → FAILED
        # ---------------------------------------------------------------
        stale_trials = (
            await session.execute(
                text(
                    """
                    SELECT id
                    FROM trials
                    WHERE status::text = 'RUNNING'
                      AND (
                          heartbeat_at IS NULL
                          OR heartbeat_at < NOW() - make_interval(mins => :stale_after_minutes)
                      )
                    """
                ),
                {"stale_after_minutes": stale_after_minutes},
            )
        ).all()

        for (trial_id,) in stale_trials:
            trial = await session.get(TrialModel, str(trial_id))
            if not trial or trial.status != TrialStatus.RUNNING:
                continue
            trial.status = TrialStatus.FAILED
            hb_failures = trial.heartbeat_failure_count or 0
            if hb_failures > 0 and trial.last_heartbeat_error:
                # Worker was alive but could not write to Postgres; this is
                # almost always a pooler / DB availability issue, not a
                # worker crash. Surface that in the error message.
                trial.error_message = (
                    "Trial was cancelled by queue cleanup because the worker heartbeat "
                    f"went stale for over {stale_after_minutes} minutes. "
                    f"The worker reported {hb_failures} heartbeat write failure(s) "
                    f"before going silent; last error was: {trial.last_heartbeat_error}"
                )
            else:
                trial.error_message = (
                    "Trial was cancelled by queue cleanup because the worker heartbeat "
                    f"went stale for over {stale_after_minutes} minutes."
                )
            trial.finished_at = trial.finished_at or utcnow()
            _clear_trial_runtime_refs(trial)
            # Record when we reaped this trial, but preserve the worker's
            # last successful heartbeat timestamp for post-mortem analysis.
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
                    "Analysis skipped because the trial was cancelled during "
                    "orphaned queue cleanup."
                )
                trial.analysis_finished_at = utcnow()

            running_stale_heartbeat_failed += 1

        await session.flush()

        # Trigger stage transitions for tasks whose trials just got failed
        for (trial_id,) in stale_trials:
            if await maybe_start_analysis_stage(session, str(trial_id)):
                tasks_progressed_to_analysis += 1

        # ---------------------------------------------------------------
        # 2. Stuck analysis: RUNNING with no recent activity → reset to QUEUED
        # ---------------------------------------------------------------
        stale_analysis_rows = (
            await session.execute(
                text(
                    """
                    SELECT id FROM trials
                    WHERE analysis_status::text = 'RUNNING'
                      AND analysis_started_at < NOW() - make_interval(mins => :stale_mins)
                    """,
                ),
                {"stale_mins": 30},
            )
        ).all()

        for (trial_id,) in stale_analysis_rows:
            trial = await session.get(TrialModel, str(trial_id))
            if not trial or trial.analysis_status != AnalysisStatus.RUNNING:
                continue
            trial.analysis_status = AnalysisStatus.QUEUED
            trial.analysis_error = None
            trial.analysis_started_at = None
            trial.analysis_finished_at = None
            _clear_analysis_runtime_refs(trial)
            stale_analysis_reset += 1

        # ---------------------------------------------------------------
        # 3. Stuck verdict: RUNNING with no recent activity → reset to QUEUED
        # ---------------------------------------------------------------
        stale_verdict_rows = (
            await session.execute(
                text(
                    """
                    SELECT id FROM tasks
                    WHERE verdict_status::text = 'RUNNING'
                      AND verdict_started_at < NOW() - make_interval(mins => :stale_mins)
                    """,
                ),
                {"stale_mins": 15},
            )
        ).all()

        for (task_id,) in stale_verdict_rows:
            task = await session.get(TaskModel, str(task_id))
            if not task or task.verdict_status != VerdictStatus.RUNNING:
                continue
            task.verdict_status = VerdictStatus.QUEUED
            task.verdict_error = None
            task.verdict_started_at = None
            task.verdict_finished_at = None
            _clear_verdict_runtime_refs(task)
            stale_verdict_reset += 1

        # ---------------------------------------------------------------
        # 4. Tasks stuck in RUNNING where all trials finished → advance pipeline
        # ---------------------------------------------------------------
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
                        WHERE tr.status IN ('PENDING', 'QUEUED', 'RUNNING', 'RETRYING')
                    ) = 0
                    """
                )
            )
        ).all()

        for (trial_id,) in tasks_ready_for_analysis:
            if trial_id and await maybe_start_analysis_stage(session, str(trial_id)):
                tasks_progressed_to_analysis += 1

        # ---------------------------------------------------------------
        # 5. Tasks stuck in ANALYZING where all analyses finished → advance
        # ---------------------------------------------------------------
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
                           OR tr.analysis_status IN ('PENDING', 'QUEUED', 'RUNNING')
                    ) = 0
                    """
                )
            )
        ).all()

        for (trial_id,) in tasks_ready_for_verdict:
            if trial_id and await maybe_start_verdict_stage(session, str(trial_id)):
                tasks_progressed_to_verdict += 1

        # ---------------------------------------------------------------
        # 6. VERDICT_PENDING tasks with no queued verdict → complete or re-queue
        # ---------------------------------------------------------------
        stale_verdict_pending = (
            await session.execute(
                text(
                    """
                    SELECT id FROM tasks
                    WHERE status = 'VERDICT_PENDING'
                      AND (verdict_status IS NULL OR verdict_status::text NOT IN ('QUEUED', 'RUNNING'))
                    """
                )
            )
        ).all()

        for (task_id,) in stale_verdict_pending:
            task = await session.get(TaskModel, str(task_id))
            if not task or task.status != TaskStatus.VERDICT_PENDING:
                continue
            if task.verdict_status in (VerdictStatus.SUCCESS, VerdictStatus.FAILED):
                task.status = TaskStatus.COMPLETED
                task.finished_at = task.finished_at or utcnow()
            else:
                task.verdict_status = VerdictStatus.QUEUED
                task.verdict_error = None
                task.verdict_started_at = None
                task.verdict_finished_at = None
                _clear_verdict_runtime_refs(task)
                stale_verdict_reset += 1

        # ---------------------------------------------------------------
        # 7. Clear stale runtime refs on terminal trials
        # ---------------------------------------------------------------
        terminal_trial_cleanup_result = cast(
            CursorResult,
            await session.execute(
                text(
                    """
                    UPDATE trials
                    SET current_worker_id = NULL,
                        current_queue_slot = NULL,
                        modal_function_call_id = NULL
                    WHERE status::text IN ('SUCCESS', 'FAILED')
                      AND (
                          current_worker_id IS NOT NULL
                          OR current_queue_slot IS NOT NULL
                          OR modal_function_call_id IS NOT NULL
                      )
                    """
                )
            ),
        )
        terminal_trial_runtime_refs_cleared = int(
            terminal_trial_cleanup_result.rowcount or 0
        )

        # ---------------------------------------------------------------
        # 8. Clear orphaned queue slot leases (no running trial on that key)
        # ---------------------------------------------------------------
        orphaned_slot_cleanup_result = cast(
            CursorResult,
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
                          FROM trials t
                          WHERE t.status::text = 'RUNNING'
                            AND t.queue_key = qs.queue_key
                      )
                    """
                )
            ),
        )
        orphaned_active_slots_cleared = int(orphaned_slot_cleanup_result.rowcount or 0)

    return {
        "running_stale_heartbeat": running_stale_heartbeat_failed,
        "stale_analysis_reset": stale_analysis_reset,
        "stale_verdict_reset": stale_verdict_reset,
        "tasks_progressed_to_analysis": tasks_progressed_to_analysis,
        "tasks_progressed_to_verdict": tasks_progressed_to_verdict,
        "terminal_trial_runtime_refs_cleared": terminal_trial_runtime_refs_cleared,
        "orphaned_active_slots_cleared": orphaned_active_slots_cleared,
        "zombie_txn_reaped": zombie_txn_reaped,
    }

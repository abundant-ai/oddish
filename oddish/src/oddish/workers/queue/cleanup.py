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

The stage-transition helpers (``maybe_start_qa_stage`` /
``maybe_advance_legacy_analyzing_task``) still run as a safety net so
tasks with all trials done can't get stuck if a single stage-transition
flush failed at handler-commit time.
"""

import asyncio
from datetime import timedelta
from typing import cast

from sqlalchemy import func, select, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from oddish.config import (
    NOP_ORACLE_QUEUE_KEY,
    ORPHANED_ANALYSIS_ERROR_PREFIX,
    settings,
)
from oddish.core.baseline_gate import GATE_SKIP_PREFIX
from oddish.core.helpers import cancel_job_by_worker
from oddish.core.tags.ownership_transfer import sweep_orphaned_tag_owners
from oddish.costs.recorder import reconcile_compute_cost_spans
from oddish.db import (
    AnalysisStatus,
    JobStatus,
    TaskModel,
    TaskStatus,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    get_session,
    utcnow,
)
from oddish.db.models import AnalyzerModel
from oddish.workers.queue.worker_job_single_job import (
    calculate_trial_retry_delay_seconds,
    classify_retry_reason,
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

# Grace before a leased queue slot is reclaimed as orphaned. A worker takes its
# slot lease just before claiming a job, so for a brief window the slot is held
# with no RUNNING worker_jobs row pointing back at it. The reconciler runs every
# few minutes, so 2 minutes comfortably clears that acquire->claim gap without
# meaningfully delaying reclamation of genuinely leaked leases.
ORPHANED_SLOT_GRACE_MINUTES = 2

# Backstop for tasks wedged in ANALYZING because a live trial never produced an
# analysis verdict. The stage-advance passes treat a live trial whose
# ``analysis_status`` is NULL as "analysis still pending", so a task with a
# FAILED trial that never had analysis enqueued (observed on ~1k pre-existing
# tasks) can never reach the verdict stage. For stale ANALYZING tasks with
# nothing analysis- or trial-side still in flight, we mark the lingering NULL
# analysis terminal (it will never run) so the normal advance carries them to
# VERDICT_PENDING; tasks with no live trials left are finalized FAILED. Only
# tasks idle longer than this are touched (so we never race a live transition,
# which completes in seconds) and we cap the batch so a large backlog drains
# over several ticks instead of one giant transaction.
STUCK_ANALYZING_MINUTES = 15
STUCK_ANALYZING_BATCH_LIMIT = 200

# Backstop for tasks wedged in VERDICT_PENDING whose QA job is gone -- e.g. it
# failed/exhausted without committing a terminal ``verdict_status`` (the old
# unguarded verdict reconstruction crashed on probe-summary trials and rolled
# back, leaving ``verdict_status='QUEUED'`` with no live worker_job). The
# previous step-4 guard keyed off ``verdict_status NOT IN ('QUEUED','RUNNING')``
# and so skipped exactly these rows, stranding them forever. We instead key off
# "no live QA/VERDICT worker_job" and re-enqueue (or finalize) them. Batched so
# a large backlog drains over several ticks instead of one giant burst.
STALE_VERDICT_PENDING_BATCH_LIMIT = 200
STUCK_ANALYZING_REASON = (
    "Analysis never produced a verdict for this trial; marked terminal by "
    "orphaned-pipeline cleanup so the task could leave the ANALYZING stage."
)

_TAG_PROJECT_ACTIVE_CONSTRAINT = "uq_worker_jobs_tag_project_active"
_TAG_PROJECT_SUPERSEDED_REASON = (
    "Stale TAG_PROJECT worker was superseded by an active follow-up for the "
    "same subject."
)

# Backstop for trials stranded with a non-terminal ``analysis_status`` by a QA
# job that died or was cancelled mid-classification. The task-level QA job
# marks one trial RUNNING at a time; a SIGKILLed/timed-out worker (or a
# cancelled ``should_store`` write) leaves that trial non-terminal with nothing
# left to finish it. Historically these accumulated forever (an incident found
# 4k+ of them rendering as phantom "running" analyses). Two arms:
#   * never-classifiable rows (superseded / SKIPPED / bulk-imported /
#     gate-skipped trials, a soft-deleted task, or a terminal task with no
#     active QA job) are finalized FAILED, stamped with the
#     orphaned-analysis sentinel so a later resurrect can reopen them;
#   * rows a future QA attempt will re-classify are moved RUNNING -> QUEUED so
#     the UI reflects "waiting", not a live classification.
# Staleness-gated well above the QA per-trial classification window so we
# never race an in-flight write, and batched so a large backlog drains over a
# few ticks instead of one giant transaction. Both arms select their rows
# FOR UPDATE SKIP LOCKED (of trials only): the sweep transaction may already
# hold task row locks, and waiting on a trial row inverts the trials-then-task
# lock order ``cancel_tasks_runs`` documents (deadlock).
ORPHANED_ANALYSIS_MINUTES = 30
ORPHANED_ANALYSIS_BATCH_LIMIT = 2000
ORPHANED_ANALYSIS_REASON = (
    ORPHANED_ANALYSIS_ERROR_PREFIX
    + "its QA job died or was cancelled and no further attempt will classify "
    "this trial; marked terminal by orphaned-pipeline cleanup."
)


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


# Display-hygiene clear of stale claim metadata on terminal trials runs in its
# own short, batched transactions rather than inline in the big reconciliation
# transaction. An unbounded ``UPDATE trials ... WHERE status IN (terminal)``
# grabs row locks in an arbitrary order and deadlocked head-on against the live
# single-job workers writing the same rows (claim sets current_worker_id; the
# dispatcher cleared it). Batching with a stable ORDER BY + FOR UPDATE SKIP
# LOCKED means we only ever lock rows we can grab immediately, in a consistent
# order, and commit each batch on its own -- so this can neither deadlock nor
# roll back the rest of the sweep.
TERMINAL_REF_CLEAR_BATCH_SIZE = 500
TERMINAL_REF_CLEAR_MAX_BATCHES = 40


async def clear_terminal_trial_runtime_refs(
    *,
    batch_size: int = TERMINAL_REF_CLEAR_BATCH_SIZE,
    max_batches: int = TERMINAL_REF_CLEAR_MAX_BATCHES,
) -> int:
    """Null out ``current_worker_id`` / ``current_queue_slot`` on terminal trials.

    Best-effort, batched, and deadlock-resistant: each batch runs in its own
    transaction using ``FOR UPDATE SKIP LOCKED`` over an ordered candidate set,
    so it never contends head-on with a worker mid-write. Stops early on the
    first batch that clears fewer than ``batch_size`` rows (nothing left) or if
    a transient DB error is hit (the next sweep retries).
    """
    total_cleared = 0
    for _ in range(max_batches):
        try:
            async with get_session() as session:
                result = cast(
                    CursorResult,
                    await session.execute(
                        text(
                            """
                            WITH victims AS (
                                SELECT id
                                FROM   trials
                                WHERE  status::text IN ('SUCCESS', 'FAILED', 'SKIPPED')
                                  AND  deleted_at IS NULL
                                  AND  (
                                      current_worker_id IS NOT NULL
                                      OR current_queue_slot IS NOT NULL
                                  )
                                ORDER BY id
                                FOR UPDATE SKIP LOCKED
                                LIMIT :batch_size
                            )
                            UPDATE trials t
                            SET    current_worker_id = NULL,
                                   current_queue_slot = NULL
                            FROM   victims v
                            WHERE  t.id = v.id
                            """
                        ),
                        {"batch_size": batch_size},
                    ),
                )
                cleared = int(result.rowcount or 0)
        except SQLAlchemyError as exc:
            console.print(
                f"[yellow]Terminal trial runtime-ref clear skipped: {exc}[/yellow]"
            )
            break

        total_cleared += cleared
        if cleared < batch_size:
            break

    return total_cleared


# TTL backstop for `trial_events` rows leaked by hard-killed workers (the
# happy-path delete lives in the worker's terminal `finally`). Runs in its own
# best-effort transaction like the other post-commit passes.
TRIAL_EVENTS_TTL_HOURS = 24


async def purge_stale_trial_events() -> int:
    try:
        async with get_session() as session:
            result = cast(
                CursorResult,
                await session.execute(
                    text(
                        """
                        DELETE FROM trial_events te
                        USING trials t
                        WHERE t.id = te.trial_id
                          AND t.finished_at IS NOT NULL
                          AND t.finished_at < NOW() - make_interval(hours => :ttl_hours)
                        """
                    ),
                    {"ttl_hours": TRIAL_EVENTS_TTL_HOURS},
                ),
            )
            return int(result.rowcount or 0)
    except SQLAlchemyError as exc:
        console.print(f"[yellow]Trial events TTL sweep skipped: {exc}[/yellow]")
        return 0


class _DomainRowLocked(Exception):
    """Domain row exists but is FOR-UPDATE-locked by settle/retry; the caller
    rolls back the job's savepoint so the whole unit retries next sweep."""


async def _locked_or_missing(session, model, subject_id: str):
    row = (
        await session.execute(
            select(model)
            .where(model.id == subject_id)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    still_there = await session.scalar(
        select(func.count()).select_from(model).where(model.id == subject_id)
    )
    if still_there:
        raise _DomainRowLocked()
    return None  # gone (or soft-deleted): nothing to mirror, keep the job CAS


async def _mirror_stale_job_to_domain_row(session, row) -> str | None:
    """Mirror a reaped worker_jobs row's terminal state onto its domain row.

    Returns the trial id when a TRIAL was mirrored FAILED (the caller triggers
    stage transitions), else None. Raises ``_DomainRowLocked`` when the domain
    row is locked by another writer.
    """
    kind = row["kind"]
    subject_id = row["subject_id"]
    if not subject_id:
        return None

    if kind == "TRIAL":
        trial = await _locked_or_missing(session, TrialModel, str(subject_id))
        if trial is None:
            return None
        if row["new_status"] == "RETRYING":
            delay_seconds = calculate_trial_retry_delay_seconds(
                attempts=int(row["attempts"]),
                error_message=row["error_message"],
            )
            retry_at = utcnow() + timedelta(seconds=delay_seconds)
            await session.execute(
                text(
                    """
                    UPDATE worker_jobs
                    SET    next_retry_at = :retry_at,
                           available_after = :retry_at
                    WHERE  id = :job_id
                    """
                ),
                {"job_id": row["id"], "retry_at": retry_at},
            )
            # Domain row goes back to RETRYING so the UI reflects "waiting for
            # another attempt". The new worker_jobs claim will bump
            # trials.status back to RUNNING via ``_prepare_trial_run``.
            trial.status = TrialStatus.RETRYING
            trial.error_message = row["error_message"]
            trial.next_retry_at = retry_at
            trial.finished_at = None
            trial.current_worker_id = None
            trial.current_queue_slot = None
            trial.stale_reaped_at = utcnow()
            console.print(
                f"metric=worker_job_stale_retry_scheduled id={row['id']} "
                f"attempts={row['attempts']}/{row['max_attempts']} "
                f"retry_reason={classify_retry_reason(row['error_message'])} "
                f"retry_delay_seconds={delay_seconds:.2f}"
            )
            return None
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
        return trial.id

    if kind == "ANALYSIS":
        # Legacy per-trial classification rows, drained across a deploy.
        trial = await _locked_or_missing(session, TrialModel, str(subject_id))
        if trial is None:
            return None
        if row["new_status"] == "FAILED":
            trial.analysis_status = AnalysisStatus.FAILED
            trial.analysis_error = row["error_message"]
            trial.analysis_finished_at = utcnow()
        else:
            # Retrying: show "queued for retry" in the UI rather than leaving
            # the row on RUNNING. The handler resets to QUEUED on next claim.
            trial.analysis_status = AnalysisStatus.QUEUED
            trial.analysis_error = row["error_message"]
        return None

    if kind == "QA":
        task = await _locked_or_missing(session, TaskModel, str(subject_id))
        if task is None:
            return None
        if row["new_status"] == "FAILED":
            task.verdict_status = VerdictStatus.FAILED
            task.verdict_error = row["error_message"]
            task.verdict_finished_at = utcnow()
            # No further QA attempt will run for this task, so any trial the
            # dead job left mid-classification would stay non-terminal forever
            # (and count as a phantom "running" analysis in the dashboard
            # pipeline). Finalize them alongside the verdict, stamped with the
            # orphaned-analysis sentinel so a later resurrect (append) can
            # reopen them. The id-selection takes trial row locks with SKIP
            # LOCKED: we already hold the task row lock, and *waiting* on a
            # trial row here inverts the trials-then-task lock order
            # ``cancel_tasks_runs`` documents (deadlock; a lock wait would
            # also stall the whole sweep). Contended rows are healed by the
            # orphan sweep instead. Raw SQL: soft-delete filter is explicit.
            await session.execute(
                text(
                    """
                    UPDATE trials
                    SET    analysis_status = 'FAILED',
                           analysis_error = :reason,
                           analysis_finished_at = NOW()
                    WHERE  id IN (
                        SELECT id
                        FROM   trials
                        WHERE  task_id = :task_id
                          AND  deleted_at IS NULL
                          AND  analysis_status IN
                                   ('PENDING', 'QUEUED', 'RUNNING')
                        FOR UPDATE SKIP LOCKED
                    )
                    """
                ),
                {
                    "task_id": task.id,
                    "reason": ORPHANED_ANALYSIS_ERROR_PREFIX
                    + (
                        row["error_message"]
                        or "task QA job failed before classifying this trial."
                    ),
                },
            )
        else:
            task.verdict_status = VerdictStatus.QUEUED
            task.verdict_error = row["error_message"]
            # The retry re-classifies anything non-terminal; requeue the rows
            # the dead attempt left in flight (and reopen orphan-finalized
            # ones) so the UI shows "queued for retry" instead of a phantom
            # in-flight classification. Shared helper: SKIP LOCKED, same
            # lock-order rationale as the FAILED arm above.
            from oddish.queue import requeue_inflight_trial_analysis

            await requeue_inflight_trial_analysis(session, task_id=task.id)
        return None

    if kind == "ANALYZER":
        analyzer = await _locked_or_missing(session, AnalyzerModel, str(subject_id))
        if analyzer is None:
            return None
        if row["new_status"] == "FAILED":
            analyzer.status = JobStatus.FAILED
            analyzer.error = row["error_message"]
            analyzer.finished_at = utcnow()
        else:
            analyzer.status = JobStatus.QUEUED
            analyzer.error = row["error_message"]
        return None

    return None


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
    zombie_txn_reaped = await reap_idle_in_transaction_zombies()

    async with get_session() as session:
        (
            worker_jobs_retried,
            worker_jobs_failed,
            reaped_trial_ids,
            worker_targets,
        ) = await _reap_stale_worker_jobs(
            session, stale_after_minutes=stale_after_minutes
        )
        tasks_progressed_to_analysis = await _advance_running_tasks_to_analysis(
            session, reaped_trial_ids
        )

        tasks_progressed_to_verdict = await _advance_legacy_analyzing_tasks(session)

        verdict_pending_completed = await _heal_stale_verdict_pending(session)

        (
            stuck_analyzing_advanced,
            stuck_analyzing_finalized,
            stuck_analysis_nulls_failed,
        ) = await _unwedge_stuck_analyzing(session)

        (
            orphaned_analysis_failed,
            orphaned_analysis_requeued,
        ) = await _reset_orphaned_trial_analysis(session)

        orphaned_active_slots_cleared = await _release_orphaned_slots(session)

        experiments_last_activity_reconciled = (
            await _reconcile_experiment_last_activity(session)
        )

        tag_projections_reconciled = await _maybe_reconcile_tag_projections(session)
        tag_owners_reassigned = await sweep_orphaned_tag_owners(session)

    try:
        modal_cost_spans_reconciled = await reconcile_compute_cost_spans()
    except Exception as exc:
        console.print(f"[yellow]Modal cost reconciliation failed: {exc}[/yellow]")
        modal_cost_spans_reconciled = 0

    # These run AFTER the outer commit so a rolled-back sweep never tears down
    # remote handles / claim metadata the DB still points at. Best-effort; the
    # provider TTL and the next sweep are the backstops.
    worker_sandboxes_terminated = await _terminate_orphaned_sandboxes(worker_targets)
    terminal_trial_runtime_refs_cleared = await clear_terminal_trial_runtime_refs()
    stale_trial_events_purged = await purge_stale_trial_events()

    return {
        "worker_jobs_retried": worker_jobs_retried,
        "worker_jobs_failed": worker_jobs_failed,
        "worker_sandboxes_terminated": worker_sandboxes_terminated,
        "tasks_progressed_to_analysis": tasks_progressed_to_analysis,
        "tasks_progressed_to_verdict": tasks_progressed_to_verdict,
        "verdict_pending_completed": verdict_pending_completed,
        "stuck_analyzing_advanced": stuck_analyzing_advanced,
        "stuck_analyzing_finalized": stuck_analyzing_finalized,
        "stuck_analysis_nulls_failed": stuck_analysis_nulls_failed,
        "orphaned_analysis_failed": orphaned_analysis_failed,
        "orphaned_analysis_requeued": orphaned_analysis_requeued,
        "terminal_trial_runtime_refs_cleared": terminal_trial_runtime_refs_cleared,
        "stale_trial_events_purged": stale_trial_events_purged,
        "orphaned_active_slots_cleared": orphaned_active_slots_cleared,
        "zombie_txn_reaped": zombie_txn_reaped,
        "experiments_last_activity_reconciled": experiments_last_activity_reconciled,
        "tag_projections_reconciled": tag_projections_reconciled,
        "tag_owners_reassigned": tag_owners_reassigned,
        "modal_cost_spans_reconciled": modal_cost_spans_reconciled,
    }


# Advisory-lock key so only one container reconciles tag projections per
# sweep. 0x7400 ~ "t" "\0" — arbitrary stable constant.
_TAG_PROJECTION_LOCK_KEY = 0x7400
# Tag-projection reconciliation must NOT run on every poll-tick (~180s).
_TAG_PROJECTION_RUN_EVERY_MINUTES = 60


async def _recompute_drifted_task_projections(session) -> int:
    """Recompute the projection for any task whose membership row was
    touched in the last hour but whose ``effective_tag_ids`` array is
    empty despite the experiment carrying a living tag. Bounded so we
    never scan the whole table."""
    from oddish.core.tags.projection import recompute_task_browse_projection

    rows = (
        await session.execute(
            text(
                """
                SELECT DISTINCT te.task_id
                FROM task_experiments te
                JOIN tag_assignments a
                  ON a.scope = 'EXPERIMENT'
                 AND a.source = 'EXPERIMENT_LIVING'
                 AND a.target_id = te.experiment_id
                 AND a.deleted_at IS NULL
                 AND a.state = 'ACTIVE'
                JOIN tasks t ON t.id = te.task_id
                WHERE te.deleted_at IS NULL
                  AND t.deleted_at IS NULL
                  AND t.updated_at > NOW() - INTERVAL '1 hour'
                  AND COALESCE(array_length(t.effective_tag_ids, 1), 0) = 0
                LIMIT 500
                """
            )
        )
    ).all()
    count = 0
    for (task_id,) in rows:
        await recompute_task_browse_projection(session, task_id=str(task_id))
        count += 1
    return count


async def _maybe_reconcile_tag_projections(session) -> int:
    """Hourly, cadence-gated, advisory-lock-guarded reconciliation.

    1. Try a transaction-scoped advisory lock (cheap; one container wins).
    2. Read ``last_full_sweep_at`` from the ``tag_projection_sweep_state``
       singleton; if it ran in the last hour, skip.
    3. Recompute drifted projections; bump the timestamp (upsert).
    """
    locked = await session.scalar(
        text("SELECT pg_try_advisory_xact_lock(:k)"),
        {"k": _TAG_PROJECTION_LOCK_KEY},
    )
    if not locked:
        return 0

    age_minutes = await session.scalar(
        text(
            """
            SELECT EXTRACT(EPOCH FROM (NOW() - last_full_sweep_at)) / 60
            FROM tag_projection_sweep_state
            WHERE id = TRUE
            """
        )
    )
    if age_minutes is not None and age_minutes < _TAG_PROJECTION_RUN_EVERY_MINUTES:
        return 0

    recomputed = await _recompute_drifted_task_projections(session)
    await session.execute(
        text(
            """
            INSERT INTO tag_projection_sweep_state (id, last_full_sweep_at)
            VALUES (TRUE, NOW())
            ON CONFLICT (id) DO UPDATE SET last_full_sweep_at = NOW()
            """
        )
    )
    return recomputed


# =============================================================================
# Reconciliation steps. Each takes the shared sweep ``session`` (so it runs in
# the one reconciliation transaction) and returns its counters. Extracted from
# the former single ~560-line ``cleanup_orphaned_queue_state`` so each phase is
# independently readable and testable; the orchestrator just sequences them.
# =============================================================================


def _is_tag_project_active_collision(exc: IntegrityError) -> bool:
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))

        diag = getattr(current, "diag", None)
        constraint_name = getattr(diag, "constraint_name", None) or getattr(
            current, "constraint_name", None
        )
        if constraint_name == _TAG_PROJECT_ACTIVE_CONSTRAINT:
            return True

        for attr in ("orig", "__cause__", "__context__"):
            nested = getattr(current, attr, None)
            if isinstance(nested, BaseException):
                pending.append(nested)

    # asyncpg wrappers have not exposed ``diag.constraint_name`` consistently
    # across versions, but their error text still includes the exact index.
    return _TAG_PROJECT_ACTIVE_CONSTRAINT in str(exc)


async def _transition_stale_worker_job(
    session,
    *,
    job_id: str,
    stale_after_minutes: int,
    force_tag_project_terminal: bool = False,
):
    return (
        (
            await session.execute(
                text(
                    """
                    WITH candidate AS (
                        SELECT id,
                               kind,
                               subject_table,
                               subject_id,
                               attempts,
                               max_attempts
                        FROM worker_jobs
                        WHERE id = :job_id
                          AND status::text = 'RUNNING'
                          AND (
                              heartbeat_at IS NULL
                              OR heartbeat_at < NOW() - make_interval(
                                  mins => :stale_after_minutes
                              )
                          )
                        FOR UPDATE
                    ),
                    decision AS (
                        SELECT candidate.id,
                               candidate.attempts < candidate.max_attempts
                                   AS has_retries,
                               (
                                   candidate.kind = 'TAG_PROJECT'
                                   AND (
                                       :force_tag_project_terminal
                                       OR EXISTS (
                                           SELECT 1
                                           FROM worker_jobs followup
                                           WHERE followup.id <> candidate.id
                                             AND followup.kind = 'TAG_PROJECT'
                                             AND followup.subject_table
                                                 IS NOT DISTINCT FROM
                                                 candidate.subject_table
                                             AND followup.subject_id
                                                 IS NOT DISTINCT FROM
                                                 candidate.subject_id
                                             AND followup.status
                                                 IN ('QUEUED', 'RETRYING')
                                       )
                                   )
                               ) AS tag_project_superseded
                        FROM candidate
                    )
                    UPDATE worker_jobs job
                    SET    status = CASE
                               WHEN decision.has_retries
                                    AND NOT decision.tag_project_superseded
                                   THEN 'RETRYING'::worker_job_status
                               ELSE 'FAILED'::worker_job_status
                           END,
                           payload = CASE
                               WHEN decision.has_retries
                                    AND NOT decision.tag_project_superseded
                                   THEN job.payload
                               ELSE job.payload - 'registry_auth_enc'
                           END,
                           stale_reaped_at = NOW(),
                           finished_at = CASE
                               WHEN decision.has_retries
                                    AND NOT decision.tag_project_superseded
                                   THEN job.finished_at
                               ELSE NOW()
                           END,
                           current_worker_id = NULL,
                           current_queue_slot = NULL,
                           modal_function_call_id = NULL,
                           error_message = CASE
                               WHEN decision.tag_project_superseded
                                   THEN :tag_project_superseded_reason
                               WHEN heartbeat_failure_count > 0
                                    AND last_heartbeat_error IS NOT NULL
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
                    FROM decision
                    WHERE job.id = decision.id
                    RETURNING job.id,
                              job.kind::text AS kind,
                              job.status::text AS new_status,
                              job.subject_table,
                              job.subject_id,
                              job.attempts,
                              job.max_attempts,
                              job.error_message,
                              job.provider,
                              job.external_id,
                              decision.tag_project_superseded
                    """
                ),
                {
                    "job_id": job_id,
                    "stale_after_minutes": stale_after_minutes,
                    "force_tag_project_terminal": force_tag_project_terminal,
                    "tag_project_superseded_reason": _TAG_PROJECT_SUPERSEDED_REASON,
                },
            )
        )
        .mappings()
        .one_or_none()
    )


async def _reap_stale_worker_jobs(
    session, *, stale_after_minutes: int
) -> tuple[int, int, list[str], set[tuple[str, str]]]:
    """Step 1 -- stale-heartbeat sweep on ``worker_jobs``.

    Transitions RUNNING rows whose heartbeat stalled to RETRYING (attempts
    remain) or FAILED (exhausted), then mirrors the terminal state onto the
    domain row. Each job is one SAVEPOINT: when the domain row is locked
    (settle/retry is writing its terminal state -- mirroring would clobber it
    and waiting would deadlock, since we hold the job lock and the holder takes
    domain-row -> worker_jobs), the whole unit rolls back and retries next
    sweep. Returns ``(retried, failed, reaped_trial_ids, worker_targets)``.
    """
    stale_candidate_ids = [
        row[0]
        for row in (
            await session.execute(
                text(
                    """
                    SELECT id FROM worker_jobs
                    WHERE  status::text = 'RUNNING'
                      AND  (
                          heartbeat_at IS NULL
                          OR heartbeat_at < NOW() - make_interval(mins => :stale_after_minutes)
                      )
                    ORDER BY id
                    """
                ),
                {"stale_after_minutes": stale_after_minutes},
            )
        ).all()
    ]

    worker_jobs_retried = 0
    worker_jobs_failed = 0
    reaped_trial_ids: list[str] = []
    worker_targets: set[tuple[str, str]] = set()

    for stale_job_id in stale_candidate_ids:
        row = None
        try:
            async with session.begin_nested():
                row = await _transition_stale_worker_job(
                    session,
                    job_id=stale_job_id,
                    stale_after_minutes=stale_after_minutes,
                )
                if row is None:
                    continue  # another actor already progressed this job

                committed_trial_id = await _mirror_stale_job_to_domain_row(session, row)
                # Flush this job's mirror WITHIN its own savepoint so the unit
                # is explicitly atomic and independent of ``begin_nested``'s
                # autoflush-on-enter timing (which, if it ever changed, could
                # let a later ``_DomainRowLocked`` rollback revert an
                # already-terminal job's domain mirror).
                await session.flush()
        except IntegrityError as exc:
            if not _is_tag_project_active_collision(exc):
                raise

            # A TAG_PROJECT enqueue can win the unique-index race after the
            # decision query's snapshot. The failed UPDATE has already rolled
            # back to its savepoint; retry only this stale job as terminal.
            async with session.begin_nested():
                row = await _transition_stale_worker_job(
                    session,
                    job_id=stale_job_id,
                    stale_after_minutes=stale_after_minutes,
                    force_tag_project_terminal=True,
                )
                if row is None:
                    continue
                committed_trial_id = await _mirror_stale_job_to_domain_row(session, row)
                await session.flush()
            console.print(
                f"metric=tag_project_stale_reap_superseded id={stale_job_id} "
                f"subject={row['subject_table']}/{row['subject_id']} "
                "reason=active_followup_race"
            )
        except _DomainRowLocked:
            console.print(
                f"metric=worker_job_stale_reap_deferred id={stale_job_id} "
                f"subject={row['subject_table']}/{row['subject_id']} "
                "reason=domain_row_locked (retrying next sweep)"
            )
            continue

        if row["new_status"] == "RETRYING":
            worker_jobs_retried += 1
        else:
            worker_jobs_failed += 1
        provider = row.get("provider")
        external_id = row.get("external_id")
        if provider and external_id:
            worker_targets.add((str(provider), str(external_id)))
        if row["new_status"] == "RETRYING" and external_id:
            # The retry must start UNLINKED: a carried-over handle points at
            # the previous attempt's sandbox, which both misdirects
            # handle-based teardown and defeats the orphan sweeper's
            # live-unlinked guard for the new attempt's pod. The old sandbox's
            # teardown target was already captured above.
            await session.execute(
                text(
                    """
                    UPDATE worker_jobs
                    SET    external_id = NULL,
                           provider = NULL
                    WHERE  id = :job_id
                    """
                ),
                {"job_id": row["id"]},
            )
        if committed_trial_id is not None:
            reaped_trial_ids.append(committed_trial_id)

    await session.flush()
    return worker_jobs_retried, worker_jobs_failed, reaped_trial_ids, worker_targets


async def _advance_running_tasks_to_analysis(
    session, reaped_trial_ids: list[str]
) -> int:
    """Steps 1b + 2 -- move RUNNING tasks whose live trials are all terminal to
    the analysis/QA stage. First for the trials we just reaped (a fresh failure
    may complete the task for the first time), then a general safety-net query
    in case a handler's own ``maybe_start_qa_stage`` never ran.
    """
    from oddish.queue import maybe_gate_llm_trials, maybe_start_qa_stage

    progressed = 0
    for trial_id in reaped_trial_ids:
        await maybe_gate_llm_trials(session, trial_id)
        if await maybe_start_qa_stage(session, trial_id):
            progressed += 1

    tasks_ready_for_analysis = (
        await session.execute(
            text(
                """
                SELECT MIN(tr.id) AS trial_id
                FROM tasks t
                JOIN trials tr ON tr.task_id = t.id
                WHERE t.status = 'RUNNING'
                  AND t.deleted_at IS NULL
                  AND tr.deleted_at IS NULL
                  AND tr.superseded_by_trial_id IS NULL
                GROUP BY t.id
                HAVING COUNT(*) FILTER (
                    WHERE tr.status IN ('PENDING', 'QUEUED', 'RUNNING', 'RETRYING')
                ) = 0
                """
            )
        )
    ).all()

    for (trial_id,) in tasks_ready_for_analysis:
        if trial_id and await maybe_start_qa_stage(session, str(trial_id)):
            progressed += 1

    # -----------------------------------------------------------------
    # 2b. Baseline gate backstop: (task_version, experiment) groups whose
    #     nop/oracle baselines are all terminal but whose LLM trials are
    #     still BLOCKED. Normally the last baseline's handler resolves the
    #     gate; this re-drives it if that handler was killed first. The gate
    #     is (task version, experiment)-scoped, so group + match BLOCKED LLM
    #     trials by (task_id, task_version_id, experiment_id) and hand it one
    #     representative baseline trial id per group. ``IS NOT DISTINCT
    #     FROM`` so a NULL version/experiment still matches itself (plain
    #     ``=`` would drop those scopes, unlike the ORM push path). Skipped
    #     entirely when the gate is off so it never touches the hot path.
    # -----------------------------------------------------------------
    # Only run the heavy grouped scan when something is actually BLOCKED.
    # Runs regardless of the feature flag so a flag rollback can't strand
    # armed trials; this cheap pre-check keeps the common (nothing-blocked)
    # case -- including flag-off prod -- off the hot reconcile path.
    any_blocked_trial = await session.scalar(
        text(
            "SELECT 1 FROM worker_jobs "
            "WHERE kind::text = 'TRIAL' AND status::text = 'BLOCKED' LIMIT 1"
        )
    )
    tasks_pending_gate = (
        (
            await session.execute(
                text(
                    """
                    SELECT MIN(base.id) AS baseline_trial_id
                    FROM trials base
                    WHERE base.queue_key = :nop_oracle_queue_key
                      AND base.deleted_at IS NULL
                      AND base.superseded_by_trial_id IS NULL
                      AND EXISTS (
                          SELECT 1
                          FROM worker_jobs wj
                          JOIN trials llm ON llm.id = wj.subject_id
                          WHERE wj.subject_table = 'trials'
                            AND wj.kind::text = 'TRIAL'
                            AND wj.status::text = 'BLOCKED'
                            AND llm.task_id = base.task_id
                            AND llm.task_version_id
                                IS NOT DISTINCT FROM base.task_version_id
                            AND llm.experiment_id
                                IS NOT DISTINCT FROM base.experiment_id
                      )
                    GROUP BY base.task_id, base.task_version_id,
                             base.experiment_id
                    HAVING COUNT(*) FILTER (
                        WHERE base.status
                            IN ('PENDING', 'QUEUED', 'RUNNING', 'RETRYING')
                    ) = 0
                    """
                ),
                {"nop_oracle_queue_key": NOP_ORACLE_QUEUE_KEY},
            )
        ).all()
        if any_blocked_trial
        else []
    )

    for (baseline_trial_id,) in tasks_pending_gate:
        if not baseline_trial_id:
            continue
        await maybe_gate_llm_trials(session, str(baseline_trial_id))
        # A FAULTY gate cancels the scope's LLM trials, which can make the
        # task "all trials done" for the first time. Advance it in the same
        # pass (the loop above already ran while they were still BLOCKED), so
        # the task isn't left RUNNING until the next cleanup cycle.
        if await maybe_start_qa_stage(session, str(baseline_trial_id)):
            progressed += 1
    return progressed


async def _advance_legacy_analyzing_tasks(session) -> int:
    """Step 3 -- legacy tasks stuck in ANALYZING (pre-QA-refactor) whose
    per-trial classifications all finished advance to the QA job."""
    from oddish.queue import maybe_advance_legacy_analyzing_task

    tasks_ready_for_verdict = (
        await session.execute(
            text(
                """
                SELECT MIN(tr.id) AS trial_id
                FROM tasks t
                JOIN trials tr ON tr.task_id = t.id
                WHERE t.status = 'ANALYZING'
                  AND t.deleted_at IS NULL
                  AND tr.deleted_at IS NULL
                  AND tr.superseded_by_trial_id IS NULL
                GROUP BY t.id
                HAVING COUNT(*) FILTER (
                    WHERE tr.status <> 'SKIPPED'
                      AND (tr.analysis_status IS NULL
                           OR tr.analysis_status IN ('PENDING', 'QUEUED', 'RUNNING'))
                ) = 0
                """
            )
        )
    ).all()

    progressed = 0
    for (trial_id,) in tasks_ready_for_verdict:
        if trial_id and await maybe_advance_legacy_analyzing_task(
            session, str(trial_id)
        ):
            progressed += 1
    return progressed


async def _heal_stale_verdict_pending(session) -> int:
    """Step 4 -- VERDICT_PENDING tasks with no LIVE QA job.

    A task is wedged here when its QA (task-level) job is gone -- it
    finished/failed/exhausted (and we missed the hook, or it rolled back before
    committing a terminal ``verdict_status``), or the task predates the unified
    refactor and never had one. The condition that matters is "no claimable
    QA/VERDICT worker_job", NOT ``verdict_status``: a row stuck at
    ``verdict_status='QUEUED'`` with no live job (the old probe-summary KeyError
    left thousands of these) would never be healed by a ``verdict_status``-keyed
    check. Re-enqueue so the dispatcher has something to claim (or finalize if
    the verdict is already terminal). ``ANALYSIS`` rows are intentionally ignored
    here -- they no longer drive the verdict. Returns the count finalized.
    """
    from oddish.queue import enqueue_qa_worker_job

    stale_verdict_pending = (
        await session.execute(
            text(
                """
                SELECT t.id
                FROM tasks t
                WHERE t.status = 'VERDICT_PENDING'
                  AND t.deleted_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM worker_jobs wj
                      WHERE wj.subject_table = 'tasks'
                        AND wj.subject_id = t.id
                        AND wj.kind::text IN ('QA', 'VERDICT')
                        AND wj.status::text IN (
                            'QUEUED', 'RETRYING', 'RUNNING', 'BLOCKED'
                        )
                  )
                ORDER BY t.updated_at ASC
                LIMIT :batch_limit
                """
            ),
            {"batch_limit": STALE_VERDICT_PENDING_BATCH_LIMIT},
        )
    ).all()

    verdict_pending_completed = 0
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
            await enqueue_qa_worker_job(session, task_id=task.id, org_id=task.org_id)
    return verdict_pending_completed


async def _unwedge_stuck_analyzing(session) -> tuple[int, int, int]:
    """Step 5 -- unwedge tasks stuck in ANALYZING by a live trial that never got
    an analysis verdict. The advance passes (2/3) treat a live trial with
    ``analysis_status`` NULL as "analysis still pending", so a task whose FAILED
    trials never had analysis enqueued sits in ANALYZING forever. For stale
    tasks with nothing analysis- or trial-side in flight, mark that lingering
    NULL analysis terminal (it will never run) and let the normal advance carry
    the task to VERDICT_PENDING; tasks with no live trials left are finalized
    FAILED. Staleness-gated and batched so we never race a live transition.
    Returns ``(advanced, finalized, nulls_failed)``.
    """
    from oddish.queue import maybe_advance_legacy_analyzing_task

    stuck_rows = (
        await session.execute(
            text(
                """
                SELECT t.id AS task_id,
                       (
                           SELECT MIN(tr.id)
                           FROM   trials tr
                           WHERE  tr.task_id = t.id
                             AND  tr.deleted_at IS NULL
                             AND  tr.superseded_by_trial_id IS NULL
                       ) AS live_trial_id
                FROM   tasks t
                WHERE  t.deleted_at IS NULL
                  AND  t.status = 'ANALYZING'
                  AND  t.updated_at < NOW() - make_interval(mins => :stale_minutes)
                  AND  NOT EXISTS (
                      SELECT 1 FROM trials a
                      WHERE  a.task_id = t.id
                        AND  a.deleted_at IS NULL
                        AND  a.superseded_by_trial_id IS NULL
                        AND  (
                            a.status IN ('PENDING', 'QUEUED', 'RUNNING', 'RETRYING')
                            OR a.analysis_status IN ('PENDING', 'QUEUED', 'RUNNING')
                        )
                  )
                ORDER BY t.updated_at ASC
                LIMIT :batch_limit
                """
            ),
            {
                "stale_minutes": STUCK_ANALYZING_MINUTES,
                "batch_limit": STUCK_ANALYZING_BATCH_LIMIT,
            },
        )
    ).all()

    if not stuck_rows:
        return 0, 0, 0

    stuck_task_ids = [row[0] for row in stuck_rows]

    # 5a. A live trial with NULL analysis blocks the advance forever (it reads
    #     as "still pending"). It will never run now, so mark it terminal;
    #     SUCCESS/FAILED analyses are left intact.
    stuck_analysis_nulls_failed = int(
        cast(
            CursorResult,
            await session.execute(
                text(
                    """
                    UPDATE trials
                    SET    analysis_status = 'FAILED',
                           analysis_error = :reason,
                           analysis_finished_at = NOW()
                    WHERE  task_id = ANY(:task_ids)
                      AND  deleted_at IS NULL
                      AND  superseded_by_trial_id IS NULL
                      AND  analysis_status IS NULL
                      AND  status <> 'SKIPPED'
                    """
                ),
                {"reason": STUCK_ANALYZING_REASON, "task_ids": stuck_task_ids},
            ),
        ).rowcount
        or 0
    )
    await session.flush()

    # 5b. With every live trial's analysis now terminal, the normal advance
    #     moves the task to VERDICT_PENDING (the verdict is computed from the
    #     surviving trials). Tasks with no live trials left have nothing to
    #     judge -> finalize FAILED.
    stuck_analyzing_advanced = 0
    no_live_trial_ids: list[str] = []
    for task_id, live_trial_id in stuck_rows:
        if live_trial_id is None:
            no_live_trial_ids.append(str(task_id))
            continue
        if await maybe_advance_legacy_analyzing_task(session, str(live_trial_id)):
            stuck_analyzing_advanced += 1

    stuck_analyzing_finalized = 0
    if no_live_trial_ids:
        stuck_analyzing_finalized = int(
            cast(
                CursorResult,
                await session.execute(
                    text(
                        """
                        UPDATE tasks
                        SET    status = 'FAILED',
                               finished_at = COALESCE(finished_at, NOW())
                        WHERE  id = ANY(:task_ids)
                          AND  deleted_at IS NULL
                          AND  status = 'ANALYZING'
                        """
                    ),
                    {"task_ids": no_live_trial_ids},
                ),
            ).rowcount
            or 0
        )

    if stuck_analyzing_advanced or stuck_analyzing_finalized:
        console.print(
            "metric=stuck_analyzing_unwedged "
            f"advanced={stuck_analyzing_advanced} "
            f"finalized={stuck_analyzing_finalized} "
            f"analysis_nulls_failed={stuck_analysis_nulls_failed}"
        )
    return (
        stuck_analyzing_advanced,
        stuck_analyzing_finalized,
        stuck_analysis_nulls_failed,
    )


async def _reset_orphaned_trial_analysis(session) -> tuple[int, int]:
    """Step 6 -- heal trials stranded with a non-terminal ``analysis_status``.

    The task-level QA job stamps ``analysis_status='RUNNING'`` one trial at a
    time as it classifies. A worker killed (SIGKILL / Modal timeout) or a
    cancelled job skips the store, so the trial stays PENDING/QUEUED/RUNNING
    with nothing left to finish it. The QA reap mirror now resets these at
    reap time; this pass is the belt-and-braces backstop for every other
    leak path (and for rows leaked before the mirror existed).

    Arm 1 finalizes rows no QA attempt will ever classify again -- superseded
    retries, SKIPPED and gate-skipped trials, bulk-imported (Sauron) rows,
    soft-deleted tasks, or trials of a terminal task with no active QA
    worker_job -- as FAILED, stamped with ``ORPHANED_ANALYSIS_ERROR_PREFIX``
    so ``requeue_inflight_trial_analysis`` can reopen them if the task is
    later resurrected by an append. A task that is merely missing its QA job
    while still VERDICT_PENDING is deliberately NOT matched:
    ``_heal_stale_verdict_pending`` (which runs earlier in this same sweep
    transaction) re-enqueues those, and the fresh job re-classifies.

    Arm 2 moves RUNNING rows whose task will get another QA pass (task not
    terminal, no QA job currently RUNNING) back to QUEUED so the dashboard
    shows "waiting for analysis" instead of a phantom live classification.

    Both arms are staleness-gated (``ORPHANED_ANALYSIS_MINUTES``, well above a
    single classification's runtime budget) and batched. Raw SQL: soft-delete
    filters are explicit. Returns ``(failed, requeued)``.
    """
    orphans_failed = int(
        cast(
            CursorResult,
            await session.execute(
                text(
                    """
                    UPDATE trials
                    SET    analysis_status = 'FAILED',
                           analysis_error = :reason,
                           analysis_finished_at = NOW()
                    WHERE  id IN (
                        SELECT tr.id
                        FROM   trials tr
                        JOIN   tasks t ON t.id = tr.task_id
                        WHERE  tr.deleted_at IS NULL
                          AND  tr.analysis_status IN
                                   ('PENDING', 'QUEUED', 'RUNNING')
                          AND  COALESCE(tr.analysis_started_at, tr.updated_at)
                                   < NOW() - make_interval(mins => :stale_minutes)
                          AND  (
                              tr.superseded_by_trial_id IS NOT NULL
                              OR tr.status = 'SKIPPED'
                              OR tr.imported_at IS NOT NULL
                              OR COALESCE(tr.error_message, '')
                                     LIKE :gate_skip_pattern
                              OR t.deleted_at IS NOT NULL
                              OR (
                                  t.status IN ('COMPLETED', 'FAILED')
                                  AND NOT EXISTS (
                                      SELECT 1
                                      FROM   worker_jobs wj
                                      WHERE  wj.subject_table = 'tasks'
                                        AND  wj.subject_id = t.id
                                        AND  wj.kind::text = 'QA'
                                        AND  wj.status::text IN (
                                            'QUEUED', 'RETRYING',
                                            'RUNNING', 'BLOCKED'
                                        )
                                  )
                              )
                          )
                        LIMIT :batch_limit
                        FOR UPDATE OF tr SKIP LOCKED
                    )
                    """
                ),
                {
                    "reason": ORPHANED_ANALYSIS_REASON,
                    "stale_minutes": ORPHANED_ANALYSIS_MINUTES,
                    "batch_limit": ORPHANED_ANALYSIS_BATCH_LIMIT,
                    "gate_skip_pattern": f"{GATE_SKIP_PREFIX}%",
                },
            ),
        ).rowcount
        or 0
    )

    orphans_requeued = int(
        cast(
            CursorResult,
            await session.execute(
                text(
                    """
                    UPDATE trials
                    SET    analysis_status = 'QUEUED'
                    WHERE  id IN (
                        SELECT tr.id
                        FROM   trials tr
                        JOIN   tasks t ON t.id = tr.task_id
                        WHERE  tr.deleted_at IS NULL
                          AND  tr.analysis_status = 'RUNNING'
                          AND  tr.superseded_by_trial_id IS NULL
                          AND  tr.imported_at IS NULL
                          AND  tr.status <> 'SKIPPED'
                          AND  COALESCE(tr.error_message, '')
                                   NOT LIKE :gate_skip_pattern
                          AND  COALESCE(tr.analysis_started_at, tr.updated_at)
                                   < NOW() - make_interval(mins => :stale_minutes)
                          AND  t.deleted_at IS NULL
                          AND  t.status NOT IN ('COMPLETED', 'FAILED')
                          AND  NOT EXISTS (
                              SELECT 1
                              FROM   worker_jobs wj
                              WHERE  wj.subject_table = 'tasks'
                                AND  wj.subject_id = t.id
                                AND  wj.kind::text = 'QA'
                                AND  wj.status::text = 'RUNNING'
                          )
                        LIMIT :batch_limit
                        FOR UPDATE OF tr SKIP LOCKED
                    )
                    """
                ),
                {
                    "stale_minutes": ORPHANED_ANALYSIS_MINUTES,
                    "batch_limit": ORPHANED_ANALYSIS_BATCH_LIMIT,
                    "gate_skip_pattern": f"{GATE_SKIP_PREFIX}%",
                },
            ),
        ).rowcount
        or 0
    )

    if orphans_failed or orphans_requeued:
        console.print(
            "metric=orphaned_trial_analysis_reset "
            f"failed={orphans_failed} requeued={orphans_requeued}"
        )
    return orphans_failed, orphans_requeued


async def _release_orphaned_slots(session) -> int:
    """Step 7 -- release queue slot leases whose owning worker is dead.

    A slot is reclaimable when no RUNNING worker_jobs row is still owned by the
    worker that holds the lease (``queue_slots.locked_by`` ==
    ``worker_jobs.current_worker_id``). This must be per-SLOT, not per-queue_key:
    the previous version only released slots when *zero* jobs were RUNNING on the
    whole queue_key, so on a busy key a single live job kept every leaked lease
    (from a SIGKILLed/preempted worker) pinned for the full ~12h lease, saturating
    the pool while only a handful of jobs ran. ``locked_at`` grace avoids racing
    the brief acquire->claim window.
    """
    result = cast(
        CursorResult,
        await session.execute(
            text(
                """
                UPDATE queue_slots qs
                SET    locked_by = NULL,
                       locked_until = NULL,
                       locked_at = NULL
                WHERE  qs.locked_by IS NOT NULL
                  AND  (
                      qs.locked_at IS NULL
                      OR qs.locked_at < NOW() - make_interval(
                          mins => :slot_grace_minutes
                      )
                  )
                  AND  NOT EXISTS (
                      SELECT 1
                      FROM   worker_jobs wj
                      WHERE  wj.status::text = 'RUNNING'
                        AND  wj.current_worker_id = qs.locked_by
                  )
                """
            ),
            {"slot_grace_minutes": ORPHANED_SLOT_GRACE_MINUTES},
        ),
    )
    return int(result.rowcount or 0)


async def _reconcile_experiment_last_activity(session) -> int:
    """Step 8 -- reconcile drift on the denormalized
    ``experiments.last_activity_at`` column. Application write paths bump it
    best-effort on task/trial inserts, so this pass only catches misses (process
    crash between insert flush and bump, etc). Bounded by a 30-minute lookback so
    it stays cheap on every sweep.
    """
    return int(
        (
            cast(
                CursorResult,
                await session.execute(
                    text(
                        """
                        UPDATE experiments e
                        SET last_activity_at = derived.last_activity_at
                        FROM (
                            SELECT
                                sub.experiment_id,
                                GREATEST(
                                    MAX(sub.task_created_at),
                                    MAX(sub.trial_created_at)
                                ) AS last_activity_at
                            FROM (
                                SELECT
                                    te.experiment_id,
                                    t.created_at AS task_created_at,
                                    NULL::timestamptz AS trial_created_at
                                FROM task_experiments te
                                JOIN tasks t ON t.id = te.task_id
                                WHERE te.deleted_at IS NULL
                                  AND t.deleted_at IS NULL
                                  AND t.created_at >= NOW() - INTERVAL '30 minutes'
                                UNION ALL
                                SELECT
                                    tr.experiment_id,
                                    NULL::timestamptz AS task_created_at,
                                    tr.created_at AS trial_created_at
                                FROM trials tr
                                WHERE tr.deleted_at IS NULL
                                  AND tr.superseded_by_trial_id IS NULL
                                  AND tr.created_at >= NOW() - INTERVAL '30 minutes'
                            ) sub
                            GROUP BY sub.experiment_id
                        ) derived
                        WHERE e.id = derived.experiment_id
                          AND e.deleted_at IS NULL
                          AND (
                              e.last_activity_at IS NULL
                              OR e.last_activity_at < derived.last_activity_at
                          )
                        """
                    )
                ),
            ).rowcount
            or 0
        )
    )


async def _terminate_orphaned_sandboxes(worker_targets: set[tuple[str, str]]) -> int:
    """Kill the orphaned sandboxes whose workers crashed. Runs AFTER the outer
    commit: a rolled-back sweep must never leave RUNNING rows pointing at
    sandboxes we already destroyed. Best-effort and concurrent; the provider's
    auto-stop / auto-delete TTL is the backstop.
    """
    if not worker_targets:
        return 0
    results = await asyncio.gather(
        *(
            cancel_job_by_worker(provider, external_id)
            for provider, external_id in worker_targets
        )
    )
    return sum(1 for ok in results if ok)

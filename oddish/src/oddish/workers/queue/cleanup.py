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
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import cast

from sqlalchemy import func, select, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError

from oddish.config import NOP_ORACLE_QUEUE_KEY, settings
from oddish.core.helpers import cancel_job_by_worker
from oddish.core.tags.ownership_transfer import sweep_orphaned_tag_owners
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
        else:
            task.verdict_status = VerdictStatus.QUEUED
            task.verdict_error = row["error_message"]
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

        orphaned_active_slots_cleared = await _release_orphaned_slots(session)

        experiments_last_activity_reconciled = (
            await _reconcile_experiment_last_activity(session)
        )

        tag_projections_reconciled = await _maybe_reconcile_tag_projections(session)
        tag_owners_reassigned = await sweep_orphaned_tag_owners(session)

    # These run AFTER the outer commit so a rolled-back sweep never tears down
    # remote handles / claim metadata the DB still points at. Best-effort; the
    # provider TTL and the next sweep are the backstops.
    worker_sandboxes_terminated = await _terminate_orphaned_sandboxes(worker_targets)
    cancelled_sandboxes_reaped = await reap_cancelled_trial_sandboxes()
    cancelled_trial_costs_settled = await settle_cancelled_trial_costs()
    terminal_trial_runtime_refs_cleared = await clear_terminal_trial_runtime_refs()

    return {
        "worker_jobs_retried": worker_jobs_retried,
        "worker_jobs_failed": worker_jobs_failed,
        "worker_sandboxes_terminated": worker_sandboxes_terminated,
        "cancelled_sandboxes_reaped": cancelled_sandboxes_reaped,
        "cancelled_trial_costs_settled": cancelled_trial_costs_settled,
        "tasks_progressed_to_analysis": tasks_progressed_to_analysis,
        "tasks_progressed_to_verdict": tasks_progressed_to_verdict,
        "verdict_pending_completed": verdict_pending_completed,
        "stuck_analyzing_advanced": stuck_analyzing_advanced,
        "stuck_analyzing_finalized": stuck_analyzing_finalized,
        "stuck_analysis_nulls_failed": stuck_analysis_nulls_failed,
        "terminal_trial_runtime_refs_cleared": terminal_trial_runtime_refs_cleared,
        "orphaned_active_slots_cleared": orphaned_active_slots_cleared,
        "zombie_txn_reaped": zombie_txn_reaped,
        "experiments_last_activity_reconciled": experiments_last_activity_reconciled,
        "tag_projections_reconciled": tag_projections_reconciled,
        "tag_owners_reassigned": tag_owners_reassigned,
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
        try:
            async with session.begin_nested():
                row = (
                    (
                        await session.execute(
                            text(
                                """
                    UPDATE worker_jobs
                    SET    status = CASE
                               WHEN attempts < max_attempts THEN 'RETRYING'::worker_job_status
                               ELSE 'FAILED'::worker_job_status
                           END,
                           payload = CASE
                               WHEN attempts < max_attempts THEN payload
                               ELSE payload - 'registry_auth_enc'
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
                    WHERE  id = :job_id
                      AND  status::text = 'RUNNING'
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
                              error_message,
                              provider,
                              external_id
                                """
                            ),
                            {
                                "job_id": stale_job_id,
                                "stale_after_minutes": stale_after_minutes,
                            },
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    continue  # another actor already progressed this job

                committed_trial_id = await _mirror_stale_job_to_domain_row(
                    session, row
                )
                # Flush this job's mirror WITHIN its own savepoint so the unit
                # is explicitly atomic and independent of ``begin_nested``'s
                # autoflush-on-enter timing (which, if it ever changed, could
                # let a later ``_DomainRowLocked`` rollback revert an
                # already-terminal job's domain mirror).
                await session.flush()
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
    return stuck_analyzing_advanced, stuck_analyzing_finalized, stuck_analysis_nulls_failed


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


# Gracefully-cancelled TRIAL workers keep their sandbox handle so Harbor's
# cancellation recovery can pull partial agent logs before stopping the env
# itself. The reap below is the backstop for workers that died mid-recovery:
# wait out the harvest window, then tear the sandbox down. The upper bound
# skips ancient pre-deploy rows whose sandboxes are long dead.
CANCELLED_SANDBOX_REAP_GRACE_MINUTES = 5
CANCELLED_SANDBOX_REAP_MAX_AGE_MINUTES = 48 * 60
CANCELLED_SANDBOX_REAP_BATCH_SIZE = 50


async def reap_cancelled_trial_sandboxes(
    *, batch_size: int = CANCELLED_SANDBOX_REAP_BATCH_SIZE
) -> int:
    """Tear down sandboxes of cancelled TRIAL jobs after the harvest grace.

    Claims each row with SKIP LOCKED and clears ``provider``/``external_id``
    in the same transaction, then terminates post-commit -- a rollback must
    never leave rows pointing at destroyed sandboxes, and a failed teardown
    is deliberately not retried (the provider TTL catches it).
    """
    targets: set[tuple[str, str]] = set()
    try:
        async with get_session() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        WITH victims AS (
                            SELECT id, provider, external_id
                            FROM   worker_jobs
                            WHERE  kind::text = 'TRIAL'
                              AND  status::text = 'CANCELLED'
                              AND  provider IS NOT NULL
                              AND  external_id IS NOT NULL
                              AND  finished_at IS NOT NULL
                              AND  finished_at < NOW() - make_interval(mins => :grace_mins)
                              AND  finished_at > NOW() - make_interval(mins => :max_age_mins)
                            ORDER BY finished_at
                            FOR UPDATE SKIP LOCKED
                            LIMIT :batch_size
                        )
                        UPDATE worker_jobs w
                        SET    provider = NULL,
                               external_id = NULL
                        FROM   victims v
                        WHERE  w.id = v.id
                        RETURNING v.provider, v.external_id
                        """
                    ),
                    {
                        "grace_mins": CANCELLED_SANDBOX_REAP_GRACE_MINUTES,
                        "max_age_mins": CANCELLED_SANDBOX_REAP_MAX_AGE_MINUTES,
                        "batch_size": batch_size,
                    },
                )
            ).all()
            targets = {
                (str(provider), str(external_id)) for provider, external_id in rows
            }
    except SQLAlchemyError as exc:
        console.print(f"[yellow]Cancelled-sandbox reap skipped: {exc}[/yellow]")
        return 0

    return await _terminate_orphaned_sandboxes(targets)


# Cancelled trials settle at the flat ``unpriced_trial_cost_usd`` floor until
# real usage arrives. The settle pass below upgrades them to measured cost
# from the artifacts the graceful-cancel harvest uploaded to S3, gated on
# the ``harvest-complete.json`` sentinel the worker writes strictly after
# the dir upload finishes (a partial upload must never settle low forever).
# Candidates are retried every sweep until the lookback expires or the
# give-up cache caps them; rows whose artifacts never materialize keep the
# floor.
CANCELLED_SETTLE_LOOKBACK_HOURS = 24
CANCELLED_SETTLE_MIN_AGE_MINUTES = 3
CANCELLED_SETTLE_BATCH_SIZE = 25
# A candidate that repeatedly yields nothing (no sentinel, fetch failure, or
# tokenless artifacts) is dropped from candidacy in this container so it
# can't monopolize the batch head for the whole 24h lookback. Warm reconcile
# containers persist the cache across ticks; a cold start retries a bounded
# amount of work.
CANCELLED_SETTLE_MAX_ATTEMPTS = 5
_CANCELLED_SETTLE_GIVEUP_CAP = 2000
_settle_attempts: dict[str, int] = {}
# Oversized artifacts are skipped rather than loaded into the reconciler's
# memory; trajectory.json on a long run can reach tens of MB.
CANCELLED_SETTLE_MAX_ARTIFACT_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class _SalvagedUsage:
    input_tokens: int | None = None
    cache_tokens: int | None = None
    cache_write_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    total_steps: int | None = None

    def has_tokens(self) -> bool:
        # Cache-only salvage is unpriceable and, written alone, would not
        # break the unsettled predicate -- require a countable token flow.
        return self.input_tokens is not None or self.output_tokens is not None


def _accumulate_agent_result(
    agent_result, totals: dict[str, int | float | None]
) -> None:
    if not isinstance(agent_result, dict):
        return

    def _acc(field: str, value, cast) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        totals[field] = cast(value) + (totals[field] or 0)

    _acc("input_tokens", agent_result.get("n_input_tokens"), int)
    _acc("cache_tokens", agent_result.get("n_cache_tokens"), int)
    _acc("output_tokens", agent_result.get("n_output_tokens"), int)
    _acc("cost_usd", agent_result.get("cost_usd"), float)


def _salvaged_usage_from_artifacts(
    files: dict[str, str], prefix: str
) -> _SalvagedUsage:
    """Extract usage from a cancelled trial's salvaged Harbor attempt dir.

    ``files`` maps S3 key -> JSON text, already scoped to a single attempt
    dir by ``_fetch_salvaged_artifacts``. Tokens come from the per-trial
    ``<trial_name>/result.json`` Harbor's cancellation recovery wrote --
    the trial-level ``agent_result`` plus, for multi-step tasks that only
    populate per-step contexts, each ``step_results[].agent_result``
    (mirroring Harbor's ``compute_token_cost_totals``). The job-level
    ``result.json`` is the runner's debug stub and is skipped by requiring
    one path segment below the prefix. ``trajectory.json`` supplies the
    cache-write split, step count, and a token fallback.
    """
    from oddish.core.harbor_artifacts import (
        _cache_write_from_final_metrics,
        _sum_cache_write_from_steps,
    )

    totals: dict[str, int | float | None] = {
        "input_tokens": None,
        "cache_tokens": None,
        "output_tokens": None,
        "cost_usd": None,
    }
    for key, body in files.items():
        relative = key[len(prefix) :]
        if not (relative.count("/") == 1 and relative.endswith("/result.json")):
            continue
        try:
            doc = json.loads(body)
        except ValueError:
            continue
        if not isinstance(doc, dict):
            continue
        agent_result = doc.get("agent_result")
        if isinstance(agent_result, dict) and any(
            v is not None for v in agent_result.values()
        ):
            _accumulate_agent_result(agent_result, totals)
        else:
            step_results = doc.get("step_results")
            for step in step_results if isinstance(step_results, list) else []:
                if isinstance(step, dict):
                    _accumulate_agent_result(step.get("agent_result"), totals)

    cache_write_tokens: int | None = None
    total_steps: int | None = None
    fallback_metrics: dict | None = None
    for key, body in files.items():
        if not key.endswith("/trajectory.json"):
            continue
        try:
            data = json.loads(body)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        steps = data.get("steps")
        steps = steps if isinstance(steps, list) else []
        final_metrics = data.get("final_metrics")
        final_metrics = final_metrics if isinstance(final_metrics, dict) else {}
        step_cache_write = _sum_cache_write_from_steps(steps)
        if step_cache_write is None:
            step_cache_write = _cache_write_from_final_metrics(final_metrics)
        if step_cache_write is not None:
            cache_write_tokens = step_cache_write + (cache_write_tokens or 0)
        if steps:
            total_steps = len(steps) + (total_steps or 0)
        if fallback_metrics is None and final_metrics:
            fallback_metrics = final_metrics

    input_tokens = totals["input_tokens"]
    cache_tokens = totals["cache_tokens"]
    output_tokens = totals["output_tokens"]
    if input_tokens is None and output_tokens is None and fallback_metrics:

        def _as_count(value) -> int | None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            return int(value)

        input_tokens = _as_count(fallback_metrics.get("total_prompt_tokens"))
        output_tokens = _as_count(fallback_metrics.get("total_completion_tokens"))
        cache_tokens = _as_count(fallback_metrics.get("total_cached_tokens"))

    return _SalvagedUsage(
        input_tokens=int(input_tokens) if input_tokens is not None else None,
        cache_tokens=int(cache_tokens) if cache_tokens is not None else None,
        cache_write_tokens=cache_write_tokens,
        output_tokens=int(output_tokens) if output_tokens is not None else None,
        cost_usd=totals["cost_usd"],
        total_steps=total_steps,
    )


async def _fetch_salvaged_artifacts(storage, prefix: str) -> dict[str, str] | None:
    """Download one attempt's result/trajectory JSONs under a trial prefix.

    Returns ``None`` when the ``harvest-complete.json`` sentinel is absent
    (upload never happened or is still in flight -- the caller retries next
    sweep, up to the give-up cap). A retried trial accretes one Harbor
    attempt dir per upload under the same prefix; only the newest attempt
    (by its result.json mtime) is read, since summing across attempts
    double-bills.
    """
    objects = await storage.list_objects_all(prefix)
    by_key = {
        obj["key"]: obj for obj in objects if isinstance(obj.get("key"), str)
    }
    if f"{prefix}harvest-complete.json" not in by_key:
        return None

    attempt_dirs: dict[str, object] = {}
    for key, obj in by_key.items():
        relative = key[len(prefix) :]
        if relative.count("/") == 1 and relative.endswith("/result.json"):
            attempt_dirs[key.rsplit("/", 1)[0] + "/"] = obj.get("last_modified")
    if not attempt_dirs:
        return {}
    newest = max(
        attempt_dirs,
        key=lambda d: (attempt_dirs[d] is not None, attempt_dirs[d], d),
    )

    files: dict[str, str] = {}
    for key, obj in by_key.items():
        if not key.startswith(newest):
            continue
        if not (key.endswith("/result.json") or key.endswith("/trajectory.json")):
            continue
        size = obj.get("size")
        if isinstance(size, int) and size > CANCELLED_SETTLE_MAX_ARTIFACT_BYTES:
            continue
        files[key] = await storage.download_text(key)
    return files


def _settle_giveup_ids() -> list[str]:
    if len(_settle_attempts) > _CANCELLED_SETTLE_GIVEUP_CAP:
        _settle_attempts.clear()
    return [
        trial_id
        for trial_id, attempts in _settle_attempts.items()
        if attempts >= CANCELLED_SETTLE_MAX_ATTEMPTS
    ]


async def settle_cancelled_trial_costs(
    *, batch_size: int = CANCELLED_SETTLE_BATCH_SIZE
) -> int:
    """Backfill measured usage/cost onto cancelled trials from S3 artifacts.

    Candidate selection and each write are separate short transactions (the
    S3 round-trips must not hold row locks). Every write re-checks the FULL
    cancelled+unsettled predicate under SKIP LOCKED -- a retry can re-claim
    the trial between the candidate select and the write, and stamping the
    old attempt's salvage onto a RUNNING row would bill the wrong run. Only
    usage columns are touched -- status/error/finished_at stay exactly as
    the cancel wrote them. Deleted trials are included to match quota's
    settled-cost sums. Rows that repeatedly yield nothing enter the
    in-memory give-up cache so they can't starve the batch head.
    """
    try:
        from oddish.db.storage import StorageClient, get_storage_client

        storage = get_storage_client()
    except Exception as exc:
        console.print(f"[yellow]Cancelled-cost settle skipped (storage): {exc}[/yellow]")
        return 0

    unsettled = (
        TrialModel.cost_usd.is_(None),
        TrialModel.input_tokens.is_(None),
        TrialModel.output_tokens.is_(None),
    )
    still_cancelled = (
        TrialModel.harbor_stage == "cancelled",
        TrialModel.started_at.isnot(None),
        TrialModel.finished_at.isnot(None),
    )
    now = utcnow()
    given_up = _settle_giveup_ids()
    try:
        async with get_session() as session:
            candidate_query = (
                select(TrialModel.id, TrialModel.model, TrialModel.trial_s3_key)
                .where(
                    *still_cancelled,
                    TrialModel.finished_at
                    >= now - timedelta(hours=CANCELLED_SETTLE_LOOKBACK_HOURS),
                    TrialModel.finished_at
                    <= now - timedelta(minutes=CANCELLED_SETTLE_MIN_AGE_MINUTES),
                    *unsettled,
                )
                .order_by(TrialModel.finished_at)
                .limit(batch_size)
                .execution_options(include_deleted=True)
            )
            if given_up:
                candidate_query = candidate_query.where(
                    TrialModel.id.notin_(given_up)
                )
            candidates = (await session.execute(candidate_query)).all()
    except SQLAlchemyError as exc:
        console.print(f"[yellow]Cancelled-cost settle skipped: {exc}[/yellow]")
        return 0

    settled = 0
    for trial_id, model_name, trial_s3_key in candidates:
        prefix = trial_s3_key or StorageClient._trial_prefix(trial_id)
        files = None
        try:
            files = await _fetch_salvaged_artifacts(storage, prefix)
        except Exception as exc:
            console.print(
                f"[yellow]Cancelled-cost settle: fetch failed for {trial_id}: {exc}[/yellow]"
            )
        usage = (
            _salvaged_usage_from_artifacts(files, prefix)
            if files is not None
            else _SalvagedUsage()
        )
        if not usage.has_tokens():
            _settle_attempts[trial_id] = _settle_attempts.get(trial_id, 0) + 1
            continue

        cost_usd = usage.cost_usd
        if cost_usd is None:
            from oddish.model_pricing import estimate_cost_usd

            cost_usd = estimate_cost_usd(
                model_name,
                usage.input_tokens,
                usage.output_tokens,
                cached_tokens=usage.cache_tokens,
                cache_write_tokens=usage.cache_write_tokens,
            )

        try:
            async with get_session() as session:
                row = (
                    await session.execute(
                        select(TrialModel)
                        .where(TrialModel.id == trial_id, *still_cancelled, *unsettled)
                        .with_for_update(skip_locked=True)
                        .execution_options(include_deleted=True)
                    )
                ).scalar_one_or_none()
                if row is None:
                    continue
                # has_tokens() guarantees input or output is non-NULL, so
                # this write always breaks the unsettled predicate and the
                # row exits candidacy even when cost stays unpriceable.
                row.input_tokens = usage.input_tokens
                row.cache_tokens = usage.cache_tokens
                row.cache_write_tokens = usage.cache_write_tokens
                row.output_tokens = usage.output_tokens
                if usage.total_steps is not None:
                    row.total_steps = usage.total_steps
                row.cost_usd = cost_usd
                settled += 1
                _settle_attempts.pop(trial_id, None)
        except SQLAlchemyError as exc:
            console.print(
                f"[yellow]Cancelled-cost settle: write failed for {trial_id}: {exc}[/yellow]"
            )

    if settled:
        console.print(
            f"[green]Settled measured cost for {settled} cancelled trial(s)[/green]"
        )
    return settled

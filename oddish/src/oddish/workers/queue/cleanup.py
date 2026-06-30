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

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError

from oddish.config import settings
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
                                WHERE  status::text IN ('SUCCESS', 'FAILED')
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
    worker_sandboxes_terminated = 0
    tasks_progressed_to_analysis = 0
    tasks_progressed_to_verdict = 0
    terminal_trial_runtime_refs_cleared = 0
    orphaned_active_slots_cleared = 0
    verdict_pending_completed = 0
    stuck_analyzing_advanced = 0
    stuck_analyzing_finalized = 0
    stuck_analysis_nulls_failed = 0

    zombie_txn_reaped = await reap_idle_in_transaction_zombies()

    # Lazy import: ``oddish.queue`` imports ``oddish.workers.jobs.enqueue``
    # which transitively imports this module, so a top-level import
    # would race with module initialization.
    from oddish.queue import (
        maybe_advance_legacy_analyzing_task,
        maybe_start_qa_stage,
    )

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
                              error_message,
                              provider,
                              external_id
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
        worker_targets: set[tuple[str, str]] = set()
        for row in stale_rows:
            if row["new_status"] == "RETRYING":
                worker_jobs_retried += 1
            else:
                worker_jobs_failed += 1

            provider = row.get("provider")
            external_id = row.get("external_id")
            if provider and external_id:
                worker_targets.add((str(provider), str(external_id)))

            kind = row["kind"]
            subject_id = row["subject_id"]
            if not subject_id:
                continue

            if kind == "TRIAL":
                trial = await session.get(TrialModel, str(subject_id))
                if trial is None:
                    continue
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
                    # Domain row goes back to RETRYING so the UI
                    # reflects "waiting for another attempt". The new
                    # worker_jobs claim will bump trials.status back
                    # to RUNNING via ``_prepare_trial_run``.
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
                # Legacy per-trial classification rows, drained across a deploy.
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

            elif kind == "QA":
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

        # Kill the orphaned sandboxes whose workers crashed
        # Best-effort and concurrent: a dead sandbox can't block the rest of the reap,
        # and the provider's auto-stop / auto-delete TTL is backstop if this fails.
        if worker_targets:
            results = await asyncio.gather(
                *(
                    cancel_job_by_worker(provider, external_id)
                    for provider, external_id in worker_targets
                )
            )
            worker_sandboxes_terminated = sum(1 for ok in results if ok)

        # Trigger stage transitions for tasks whose trials just got
        # failed, in case the failure marks the task "all trials done"
        # for the first time.
        for trial_id in stale_trial_ids:
            if await maybe_start_qa_stage(session, trial_id):
                tasks_progressed_to_analysis += 1

        # -----------------------------------------------------------------
        # 2. Tasks stuck in RUNNING where all trials finished -> advance.
        #    Safety net in case a handler's ``maybe_start_qa_stage``
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
                tasks_progressed_to_analysis += 1

        # -----------------------------------------------------------------
        # 3. Legacy tasks stuck in ANALYZING (pre-QA-refactor) where all
        #    per-trial classifications finished -> advance to the QA job.
        # -----------------------------------------------------------------
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
                        WHERE tr.analysis_status IS NULL
                           OR tr.analysis_status IN ('PENDING', 'QUEUED', 'RUNNING')
                    ) = 0
                    """
                )
            )
        ).all()

        for (trial_id,) in tasks_ready_for_verdict:
            if trial_id and await maybe_advance_legacy_analyzing_task(
                session, str(trial_id)
            ):
                tasks_progressed_to_verdict += 1

        # -----------------------------------------------------------------
        # 4. VERDICT_PENDING tasks with no LIVE QA job.
        #    A task is wedged here when its QA (task-level) job is gone --
        #    it finished/failed/exhausted (and we missed the hook, or it
        #    rolled back before committing a terminal ``verdict_status``),
        #    or the task predates the unified refactor and never had one.
        #    The condition that matters is "no claimable QA/VERDICT
        #    worker_job", NOT ``verdict_status``: a row stuck at
        #    ``verdict_status='QUEUED'`` with no live job (the old
        #    probe-summary KeyError left thousands of these) would never be
        #    healed by a ``verdict_status``-keyed check. Re-enqueue so the
        #    dispatcher has something to claim (or finalize if the verdict
        #    is already terminal). ``ANALYSIS`` rows are intentionally
        #    ignored here -- they no longer drive the verdict.
        # -----------------------------------------------------------------
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

        from oddish.queue import enqueue_qa_worker_job

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
                await enqueue_qa_worker_job(
                    session, task_id=task.id, org_id=task.org_id
                )

        # -----------------------------------------------------------------
        # 5. Unwedge tasks stuck in ANALYZING by a live trial that never got
        #    an analysis verdict. The advance passes (2/3) treat a live trial
        #    with analysis_status NULL as "analysis still pending", so a task
        #    whose FAILED trials never had analysis enqueued sits in ANALYZING
        #    forever. For stale tasks with nothing analysis- or trial-side in
        #    flight, mark that lingering NULL analysis terminal (it will never
        #    run) and let the normal advance carry the task to VERDICT_PENDING;
        #    tasks with no live trials left are finalized FAILED. Staleness-
        #    gated and batched so we never race a live transition.
        # -----------------------------------------------------------------
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

        if stuck_rows:
            stuck_task_ids = [row[0] for row in stuck_rows]

            # 5a. A live trial with NULL analysis blocks the advance forever
            #     (it reads as "still pending"). It will never run now, so mark
            #     it terminal; SUCCESS/FAILED analyses are left intact.
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
                            """
                        ),
                        {"reason": STUCK_ANALYZING_REASON, "task_ids": stuck_task_ids},
                    ),
                ).rowcount
                or 0
            )
            await session.flush()

            # 5b. With every live trial's analysis now terminal, the normal
            #     advance moves the task to VERDICT_PENDING (the verdict is
            #     computed from the surviving trials). Tasks with no live
            #     trials left have nothing to judge -> finalize FAILED.
            no_live_trial_ids: list[str] = []
            for task_id, live_trial_id in stuck_rows:
                if live_trial_id is None:
                    no_live_trial_ids.append(str(task_id))
                    continue
                if await maybe_advance_legacy_analyzing_task(
                    session, str(live_trial_id)
                ):
                    stuck_analyzing_advanced += 1

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

        # -----------------------------------------------------------------
        # 6. (moved) Clearing stale claim metadata on terminal trials is
        #    pure display-layer hygiene and used to deadlock against live
        #    workers when run as one unbounded UPDATE inside this big
        #    transaction. It now runs after this transaction commits, in
        #    its own batched, SKIP-LOCKED helper -- see
        #    ``clear_terminal_trial_runtime_refs`` below.
        # -----------------------------------------------------------------

        # -----------------------------------------------------------------
        # 7. Release queue slot leases whose owning worker is dead.
        #    A slot is reclaimable when no RUNNING worker_jobs row is
        #    still owned by the worker that holds the lease
        #    (``queue_slots.locked_by`` == ``worker_jobs.current_worker_id``).
        #
        #    This must be per-SLOT, not per-queue_key: the previous
        #    version only released slots when *zero* jobs were RUNNING on
        #    the whole queue_key, so on a busy key a single live job kept
        #    every leaked lease (from a SIGKILLed/preempted worker) pinned
        #    for the full ~12h lease. The slot pool then saturated while
        #    only a handful of jobs actually ran, and the dispatcher --
        #    which budgets on the (low) RUNNING count -- kept spawning
        #    workers that all failed to acquire a slot.
        #
        #    ``locked_at`` grace avoids racing the brief acquire->claim
        #    window: a freshly-acquired slot hasn't had its owning job
        #    flip to RUNNING yet, so we leave very recent leases alone.
        # -----------------------------------------------------------------
        orphaned_slot_cleanup_result = cast(
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
        orphaned_active_slots_cleared = int(orphaned_slot_cleanup_result.rowcount or 0)

        # -----------------------------------------------------------------
        # 8. Reconcile drift on the denormalized
        #    ``experiments.last_activity_at`` column. Application
        #    write paths bump it best-effort on task/trial inserts,
        #    so this pass only catches misses (process crash between
        #    insert flush and bump, etc). Bounded by a 30-minute
        #    lookback so it stays cheap on every sweep.
        # -----------------------------------------------------------------
        experiments_last_activity_reconciled = int(
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
                )
            ).rowcount
            or 0
        )

        tag_projections_reconciled = await _maybe_reconcile_tag_projections(session)
        tag_owners_reassigned = await sweep_orphaned_tag_owners(session)

    # Display-hygiene clear of terminal-trial claim metadata runs *after* the
    # main reconciliation transaction commits, in its own batched / SKIP-LOCKED
    # transactions, so it can never deadlock against live workers or roll back
    # the reconciliation work above (see note in step 6).
    terminal_trial_runtime_refs_cleared = await clear_terminal_trial_runtime_refs()

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

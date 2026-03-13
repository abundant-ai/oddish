"""
Oddish Cloud - Modal Worker

Scheduled function that polls PGQueuer and executes Harbor trials.
Uses OSS oddish worker logic - wraps with Modal scheduling.

Harbor Execution:
- Oddish Cloud runs on Modal, where Docker-in-Docker is not available.
- Only "modal" and "daytona" environments are allowed on cloud backend.

Pipeline stages:
- trial jobs: execute Harbor trial runs
- analysis jobs: classify trial outcomes + enqueue verdict when complete
- verdict jobs: synthesize trial analyses into a task verdict
"""

import asyncio
from datetime import timedelta
import json
import os
from uuid import uuid4

import anyio
import asyncpg
import modal
from rich.console import Console
from harbor.models.environment_type import EnvironmentType
from sqlalchemy import select, text
from oddish.config import Settings
from modal_app import (
    MAX_WORKERS_PER_POLL,
    POLL_INTERVAL_SECONDS,
    MODEL_CONCURRENCY_DEFAULT,
    VOLUME_MOUNT_PATH,
    WORKER_BUFFER_CONTAINERS,
    WORKER_MAX_CONTAINERS,
    WORKER_MIN_CONTAINERS,
    WORKER_SCALEDOWN_WINDOW_SECONDS,
    WORKER_TIMEOUT_SECONDS,
    app,
    image,
    runtime_secrets,
    volume,
)
from pgqueuer import buffers, helpers
from pgqueuer.db import AsyncpgDriver
from pgqueuer.models import Context, Job
from pgqueuer.qm import QueueManager

from oddish.config import settings
from oddish.db import (
    AnalysisStatus,
    TaskModel,
    TrialModel,
    TrialStatus,
    close_database_connections,
    get_pool,
    get_session,
    reconfigure_database_connections,
    utcnow,
)
from oddish.queue import cancel_pgqueuer_jobs_for_trials, maybe_start_analysis_stage
from oddish.workers.queue.dispatch_planner import (
    build_spawn_plan,
    discover_active_queue_keys,
    get_queue_counts,
)
from oddish.workers.queue import (
    register_queue_entrypoints,
    run_analysis_job,
    run_trial_job,
    run_verdict_job,
)

console = Console()
ALLOWED_CLOUD_ENVIRONMENTS = {EnvironmentType.MODAL, EnvironmentType.DAYTONA}
ORPHANED_STATE_STALE_AFTER_MINUTES = 10


# =============================================================================
# GitHub Integration
# =============================================================================


async def _notify_github_trial(trial_id: str) -> None:
    """Notify GitHub of trial completion."""
    try:
        from integrations.github import notify_trial_update

        await notify_trial_update(trial_id)
    except Exception as e:
        console.print(f"[yellow]GitHub notification failed (trial): {e}[/yellow]")


async def _notify_github_analysis(trial_id: str) -> None:
    """Notify GitHub of analysis completion."""
    try:
        from integrations.github import notify_analysis_update

        await notify_analysis_update(trial_id)
    except Exception as e:
        console.print(f"[yellow]GitHub notification failed (analysis): {e}[/yellow]")


async def _notify_github_verdict(task_id: str) -> None:
    """Notify GitHub of verdict completion."""
    try:
        from integrations.github import notify_verdict_update

        await notify_verdict_update(task_id)
    except Exception as e:
        console.print(f"[yellow]GitHub notification failed (verdict): {e}[/yellow]")


async def _configure_storage_paths() -> None:
    """Configure storage paths to use Modal Volume."""
    # Patch ClassVars at runtime (since they're not env-configurable)
    Settings.local_storage_dir = f"{VOLUME_MOUNT_PATH}/tasks"
    Settings.harbor_jobs_dir = f"{VOLUME_MOUNT_PATH}/harbor"
    Settings.harbor_environment = _get_default_cloud_environment().value
    # Keep pools small: each worker processes one job.
    # Modal can burst many containers at once, so keep both SQLAlchemy and
    # asyncpg pools tiny to avoid exhausting Supabase connection limits.
    Settings.db_pool_min_size = 1
    Settings.db_pool_max_size = 2
    Settings.db_pool_size = 1
    Settings.db_pool_max_overflow = 0
    settings.asyncpg_pool_min_size = 1
    settings.asyncpg_pool_max_size = 1
    settings.default_model_concurrency = MODEL_CONCURRENCY_DEFAULT

    # Modal containers are frequently reused. Rebuild the DB clients here so the
    # smaller worker pool sizes actually take effect for this invocation.
    await reconfigure_database_connections()

    os.makedirs(Settings.local_storage_dir, exist_ok=True)
    os.makedirs(Settings.harbor_jobs_dir, exist_ok=True)

    console.print(f"[dim]Storage: {Settings.local_storage_dir}[/dim]")
    console.print(f"[dim]Harbor jobs: {Settings.harbor_jobs_dir}[/dim]")
    console.print(f"[dim]Default environment: {Settings.harbor_environment}[/dim]")


def _get_default_cloud_environment() -> EnvironmentType:
    return EnvironmentType.MODAL


async def _enforce_trial_environment(trial_id: str) -> None:
    """
    Ensure trial env stays within allowed cloud sandboxes.

    If an unsupported env (e.g. docker) is stored on a trial, rewrite it to the
    configured cloud default so worker execution never tries disallowed backends.
    """
    default_env = _get_default_cloud_environment().value
    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)
        if not trial:
            return
        current = (trial.environment or "").strip().lower()

        if not current:
            trial.environment = default_env
            await session.commit()
            return

        if current not in {env.value for env in ALLOWED_CLOUD_ENVIRONMENTS}:
            console.print(
                f"[yellow]Overriding disallowed trial env {trial.environment!r} -> {default_env!r} (trial_id={trial_id})[/yellow]"
            )
            trial.environment = default_env
            await session.commit()


# =============================================================================
# Queue-key slot leases (transaction-safe concurrency control)
# =============================================================================


async def _ensure_queue_slots(queue_key: str, limit: int) -> None:
    """Ensure queue slot rows exist up to the configured limit."""
    if limit <= 0:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO queue_slots (queue_key, slot)
            SELECT $1, slot
            FROM generate_series(0, $2 - 1) AS slot
            ON CONFLICT DO NOTHING
            """,
            queue_key,
            limit,
        )


async def _acquire_queue_slot(
    *,
    queue_key: str,
    limit: int,
    worker_id: str,
    lease_seconds: int,
) -> int | None:
    """Acquire a queue slot lease without holding a session connection."""
    if limit <= 0:
        return None
    await _ensure_queue_slots(queue_key, limit)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                WITH candidate AS (
                    SELECT queue_key, slot
                    FROM queue_slots
                    WHERE queue_key = $1
                      AND slot < $2
                      AND (locked_until IS NULL OR locked_until <= NOW())
                    ORDER BY slot
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE queue_slots
                SET locked_by = $3,
                    locked_until = NOW() + make_interval(secs => $4)
                FROM candidate
                WHERE queue_slots.queue_key = candidate.queue_key
                  AND queue_slots.slot = candidate.slot
                RETURNING queue_slots.slot
                """,
                queue_key,
                limit,
                worker_id,
                lease_seconds,
            )
    if row is None:
        return None
    return int(row["slot"])


async def _release_queue_slot(
    *,
    queue_key: str,
    slot: int,
    worker_id: str,
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE queue_slots
            SET locked_by = NULL,
                locked_until = NULL
            WHERE queue_key = $1
              AND slot = $2
              AND locked_by = $3
            """,
            queue_key,
            slot,
            worker_id,
        )


async def _cleanup_stale_queue_slots() -> int:
    """Clear expired slot leases so admin views stay accurate."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE queue_slots
            SET locked_by = NULL,
                locked_until = NULL
            WHERE locked_by IS NOT NULL
              AND locked_until IS NOT NULL
              AND locked_until <= NOW()
            """
        )
    # asyncpg returns command tag strings like: "UPDATE <n>"
    try:
        return int(str(result).split()[-1])
    except Exception:
        return 0


def _orphan_cleanup_error(
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


async def _cleanup_orphaned_queue_state(
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
                error_message = _orphan_cleanup_error(
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


# =============================================================================
# Queue Manager Factory (Modal-specific)
# =============================================================================


async def create_single_job_queue_manager(
    queue_key: str,
    *,
    worker_id: str,
    queue_slot: int,
) -> tuple[QueueManager, asyncpg.Connection]:
    """
    Create QueueManager configured to process exactly ONE job then exit.

    Each Modal worker container runs this to claim and process a single job.
    PGQueuer's SKIP LOCKED ensures no duplicate processing across workers.
    """
    # Use a dedicated connection for the one-shot worker. This avoids
    # LISTEN/NOTIFY listener state inside an asyncpg pool and keeps connection
    # usage predictable on Supabase/Modal.
    connection = await asyncpg.connect(
        settings.asyncpg_url,
        statement_cache_size=0,
    )
    qm = QueueManager(AsyncpgDriver(connection))

    retry_timer = timedelta(minutes=settings.trial_retry_timer_minutes)

    # Define job handler that signals shutdown after processing
    async def handle_queue_job(job: Job, queue_key: str) -> None:
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

        # Backwards-compat: older payloads may not include job_type, but do include trial_id.
        if job_type is None and payload.get("trial_id"):
            job_type = "trial"
            console.print(
                f"[yellow]Job missing job_type; treating as trial (queue_key={queue_key}, job_id={job.id})[/yellow]"
            )

        console.print(
            f"[cyan]Processing job_type={job_type} job_id={job.id} (queue_key={queue_key})[/cyan]"
        )

        try:
            if job_type == "trial":
                if not payload.get("trial_id"):
                    raise ValueError(
                        f"Trial job missing trial_id (queue_key={queue_key}, job_id={job.id})"
                    )
                trial_id = payload["trial_id"]
                await _enforce_trial_environment(trial_id)
                await run_trial_job(
                    job,
                    queue_key=queue_key,
                    worker_id=worker_id,
                    queue_slot=queue_slot,
                )
                # Notify GitHub of trial completion
                await _notify_github_trial(trial_id)

            elif job_type == "analysis":
                if not payload.get("trial_id"):
                    raise ValueError(
                        f"Analysis job missing trial_id (queue_key={queue_key}, job_id={job.id})"
                    )
                trial_id = payload["trial_id"]
                await run_analysis_job(job, queue_key=queue_key)
                # Notify GitHub of analysis completion
                await _notify_github_analysis(trial_id)

            elif job_type == "verdict":
                if not payload.get("task_id"):
                    raise ValueError(
                        f"Verdict job missing task_id (queue_key={queue_key}, job_id={job.id})"
                    )
                task_id = payload["task_id"]
                await run_verdict_job(job, queue_key=queue_key)
                # Notify GitHub of verdict completion
                await _notify_github_verdict(task_id)

            else:
                raise ValueError(
                    f"Unknown job_type={job_type!r} (queue_key={queue_key}, job_id={job.id})"
                )
        finally:
            # Signal shutdown after processing one job
            console.print(f"[green]Job {job.id} complete, signaling shutdown[/green]")
            qm.shutdown.set()

    # Register queue entrypoint (concurrency=1 per worker since each worker handles 1 job)
    register_queue_entrypoints(
        qm,
        queue_keys=(queue_key,),
        concurrency_limits={queue_key: 1},
        retry_timer=retry_timer,
        handler=handle_queue_job,
    )

    return qm, connection


async def _run_single_job_without_listener(qm: QueueManager) -> bool:
    """Dequeue and dispatch at most one job without starting pgqueuer listeners."""
    await qm.verify_structure()
    heartbeat_buffer_timeout = helpers.retry_timer_buffer_timeout(
        [x.parameters.retry_timer for x in qm.entrypoint_registry.values()]
    )

    async with (
        buffers.JobStatusLogBuffer(
            max_size=1,
            callback=qm.queries.log_jobs,
        ) as jbuff,
        buffers.HeartbeatBuffer(
            max_size=1,
            timeout=heartbeat_buffer_timeout / 4,
            callback=qm.queries.update_heartbeat,
        ) as hbuff,
        buffers.RequestsPerSecondBuffer(
            max_size=1,
            callback=qm.update_rps_stats,
        ) as rpsbuff,
        qm.connection,
    ):
        async for job in qm.fetch_jobs(batch_size=1, global_concurrency_limit=1):
            await rpsbuff.add(job.entrypoint)
            qm.job_context[job.id] = Context(
                cancellation=anyio.CancelScope(),
                resources=qm.resources,
            )
            await qm._dispatch(job, jbuff, hbuff)
            return True

    return False


# =============================================================================
# Modal Worker Functions
# =============================================================================


@app.function(
    image=image,
    volumes={VOLUME_MOUNT_PATH: volume},
    secrets=runtime_secrets,
    min_containers=WORKER_MIN_CONTAINERS,
    buffer_containers=WORKER_BUFFER_CONTAINERS,
    scaledown_window=WORKER_SCALEDOWN_WINDOW_SECONDS,
    max_containers=WORKER_MAX_CONTAINERS,
    timeout=WORKER_TIMEOUT_SECONDS,
    memory=1024,  # 1GB memory to prevent OOM issues
)
async def process_single_job(queue_key: str):
    """
    Process exactly ONE job from the queue.

    This function:
    1. Claims one job using PGQueuer (atomic SKIP LOCKED)
    2. Processes the job completely (trial, analysis, or verdict)
    3. Exits after completion

    Multiple instances run in parallel - PGQueuer ensures no duplicates.
    Each worker gets the full timeout budget for its single job.
    """
    console.print(f"[cyan]Job worker starting (queue_key={queue_key})...[/cyan]")
    await _configure_storage_paths()

    worker_id = f"{queue_key}-{uuid4().hex[:12]}"
    lock_slot: int | None = None
    qm_connection: asyncpg.Connection | None = None

    try:
        queue_limit = settings.get_model_concurrency(queue_key)
        if queue_limit <= 0:
            console.print(
                f"[dim]Queue limit is {queue_limit} (queue_key={queue_key}), exiting[/dim]"
            )
            return
        lock_slot = await _acquire_queue_slot(
            queue_key=queue_key,
            limit=queue_limit,
            worker_id=worker_id,
            lease_seconds=WORKER_TIMEOUT_SECONDS + 30,
        )
        if lock_slot is None:
            console.print(
                f"metric=queue_lock_contention queue_key={queue_key} limit={queue_limit}"
            )
            console.print(
                f"[dim]No queue slots available (queue_key={queue_key}), exiting[/dim]"
            )
            return
        console.print(
            f"metric=queue_lock_acquired queue_key={queue_key} "
            f"slot={lock_slot + 1} limit={queue_limit}"
        )
        console.print(
            f"[dim]Acquired queue slot {lock_slot + 1}/{queue_limit} (queue_key={queue_key})[/dim]"
        )

        qm, qm_connection = await create_single_job_queue_manager(
            queue_key=queue_key,
            worker_id=worker_id,
            queue_slot=lock_slot,
        )

        job_found = await _run_single_job_without_listener(qm)
        if not job_found:
            console.print(
                f"[dim]No job available after slot acquisition (queue_key={queue_key})[/dim]"
            )

    except asyncio.CancelledError:
        console.print("[yellow]Worker cancelled[/yellow]")
        raise
    except Exception as e:
        console.print(f"[red]Worker error: {e}[/red]")
        raise
    finally:
        if qm_connection is not None:
            await qm_connection.close()
        if lock_slot is not None:
            await _release_queue_slot(
                queue_key=queue_key,
                slot=lock_slot,
                worker_id=worker_id,
            )
        await close_database_connections()
        console.print("[green]Job worker complete[/green]")


@app.function(
    image=image,
    volumes={VOLUME_MOUNT_PATH: volume},
    secrets=runtime_secrets,
    timeout=60,  # Dispatcher is lightweight, should complete quickly
    schedule=modal.Period(seconds=POLL_INTERVAL_SECONDS),
)
async def poll_queue():
    """
    Queue-aware dispatcher that spawns job workers based on queue depth.

    This function:
    1. Runs every 30 seconds (via Modal schedule)
    2. Checks queued + running counts per queue key
    3. Spawns job workers without exceeding queue-key concurrency
    4. Each worker processes exactly one job

    Benefits:
    - Jobs start processing immediately (no waiting for next poll)
    - Each job gets full timeout budget in its own container
    - Parallelism scales with queue depth (up to MAX_WORKERS_PER_POLL per cycle)
    """
    console.print("[cyan]Queue dispatcher starting...[/cyan]")
    await _configure_storage_paths()

    try:
        stale_cleared = await _cleanup_stale_queue_slots()
        if stale_cleared > 0:
            console.print(f"metric=queue_lock_stale_cleared count={stale_cleared}")
            console.print(
                f"[dim]Cleared {stale_cleared} stale queue slot lock(s)[/dim]"
            )

        cleanup_counts = await _cleanup_orphaned_queue_state()
        if any(cleanup_counts.values()):
            console.print(
                "metric=orphaned_queue_cleanup "
                + " ".join(f"{key}={value}" for key, value in cleanup_counts.items())
            )
            console.print(
                "[yellow]Cancelled orphaned queue state:[/yellow] "
                + ", ".join(
                    f"{key}={value}" for key, value in cleanup_counts.items() if value > 0
                )
            )

        queue_keys = await discover_active_queue_keys()
        queue_counts = await get_queue_counts(queue_keys)
        concurrency_limits = {
            queue_key: settings.get_model_concurrency(queue_key)
            for queue_key in queue_keys
        }

        for queue_key in queue_keys:
            queued = queue_counts.get(queue_key, {}).get("queued", 0)
            running = queue_counts.get(queue_key, {}).get("picked", 0)
            limit = concurrency_limits.get(queue_key, 0)
            console.print(
                f"[dim]{queue_key}: queued={queued} running={running} limit={limit}[/dim]"
            )

        console.print(f"[dim]Spawn cap per poll: {MAX_WORKERS_PER_POLL}[/dim]")

        spawn_plan = build_spawn_plan(
            queue_counts=queue_counts,
            concurrency_limits=concurrency_limits,
            max_workers=MAX_WORKERS_PER_POLL,
        )

        if not spawn_plan:
            console.print("[dim]No queue capacity available, exiting[/dim]")
            return

        console.print(f"[green]Spawning {len(spawn_plan)} job worker(s)...[/green]")

        # Use Modal's async spawn interface inside this async function to avoid
        # blocking the event loop and spurious AsyncUsageWarning noise.
        await asyncio.gather(
            *(process_single_job.spawn.aio(queue_key=queue_key) for queue_key in spawn_plan)
        )
        for i, queue_key in enumerate(spawn_plan, start=1):
            console.print(
                f"[dim]Spawned worker {i}/{len(spawn_plan)} (queue_key={queue_key})[/dim]"
            )

        console.print(f"[green]Dispatched {len(spawn_plan)} workers[/green]")

    except OSError as e:
        # Transient network/DNS errors (e.g. socket.gaierror) should not
        # crash the scheduled function -- the next poll in 30s will retry.
        console.print(
            f"[yellow]Dispatcher skipped (transient network error): {e}[/yellow]"
        )
    except Exception as e:
        console.print(f"[red]Dispatcher error: {e}[/red]")
        raise
    finally:
        await close_database_connections()
        console.print("[green]Dispatcher complete[/green]")

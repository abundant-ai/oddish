from oddish.config import Settings

# Worker containers process one job each; keep DB pools minimal to avoid
# exhausting connection limits when Modal bursts many containers.
Settings.db_pool_size = 1
Settings.db_pool_max_overflow = 0

import asyncio
from uuid import uuid4

import modal

from cloud_policy import enforce_trial_environment
from modal_app import (
    MAX_WORKERS_PER_POLL,
    POLL_INTERVAL_SECONDS,
    WORKER_BUFFER_CONTAINERS,
    WORKER_MAX_CONTAINERS,
    WORKER_MIN_CONTAINERS,
    WORKER_SCALEDOWN_WINDOW_SECONDS,
    WORKER_TIMEOUT_SECONDS,
    app,
    image,
    runtime_secrets,
    worker_volumes,
)
from oddish.config import settings
from oddish.db import close_database_connections, WorkerJobKind
from oddish.workers.jobs import ensure_builtin_handlers_registered
from oddish.workers.queue.cleanup import cleanup_orphaned_queue_state
from oddish.workers.queue.slots import (
    acquire_queue_slot,
    cleanup_stale_queue_slots,
    release_queue_slot,
)
from oddish.workers.queue.worker_job_dispatcher import (
    build_spawn_plan,
    discover_active_worker_job_queue_keys,
    get_worker_job_queue_counts,
)
from oddish.workers.queue.worker_job_single_job import run_single_worker_job

from .github import notify_github_analysis, notify_github_trial, notify_github_verdict
from .runtime import configure_storage_paths, console

# Register TRIAL / ANALYSIS / VERDICT handlers against the unified
# registry as soon as this module loads in a worker container. The
# dispatcher and single-job runner also call this defensively, but
# doing it here makes the startup order explicit for readers.
ensure_builtin_handlers_registered()


# Post-success hooks: fired after the worker_jobs row is in SUCCESS
# state. Mirrors the ``on_trial_complete`` / ``on_analysis_complete`` /
# ``on_verdict_complete`` hooks the legacy dispatcher passed through
# ``run_single_job``. Hook exceptions are swallowed by the runner so a
# GitHub API hiccup never corrupts scheduling state.
_POST_SUCCESS_HOOKS = {
    WorkerJobKind.TRIAL: notify_github_trial,
    WorkerJobKind.ANALYSIS: notify_github_analysis,
    WorkerJobKind.VERDICT: notify_github_verdict,
}


@app.function(
    image=image,
    volumes=worker_volumes,
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
    Process exactly ONE ``worker_jobs`` row from the unified queue.

    1. Acquires a queue-key concurrency slot (``queue_slots``)
    2. Claims one row via ``FOR UPDATE SKIP LOCKED`` on ``worker_jobs``
    3. Dispatches to the handler registered for the row's ``kind``
    4. Records the terminal outcome on the ``worker_jobs`` row; the
       handler mirrors it back to domain tables (``trials`` / ``tasks``)

    Each worker gets the full timeout budget for its single job.
    """
    console.print(f"[cyan]Job worker starting (queue_key={queue_key})...[/cyan]")
    await configure_storage_paths()

    fc_id: str | None = None
    try:
        fc_id = modal.current_function_call_id()
    except Exception:
        pass
    if fc_id:
        console.print(f"[dim]Modal function call: {fc_id}[/dim]")

    worker_id = f"{queue_key}-{uuid4().hex[:12]}"
    lock_slot: int | None = None

    try:
        queue_limit = settings.get_model_concurrency(queue_key)
        if queue_limit <= 0:
            console.print(
                f"[dim]Queue limit is {queue_limit} (queue_key={queue_key}), exiting[/dim]"
            )
            return
        lock_slot = await acquire_queue_slot(
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

        job_found = await run_single_worker_job(
            queue_key=queue_key,
            worker_id=worker_id,
            queue_slot=lock_slot,
            modal_function_call_id=fc_id,
            post_success_hooks=_POST_SUCCESS_HOOKS,
        )
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
        if lock_slot is not None:
            await release_queue_slot(
                queue_key=queue_key,
                slot=lock_slot,
                worker_id=worker_id,
            )
        await close_database_connections()
        console.print("[green]Job worker complete[/green]")


@app.function(
    image=image,
    volumes=worker_volumes,
    secrets=runtime_secrets,
    timeout=60,  # Dispatcher is lightweight, should complete quickly
    max_containers=1,  # Keep the scheduled dispatcher singleton-ish.
    schedule=modal.Period(seconds=POLL_INTERVAL_SECONDS),
)
async def poll_queue():
    """
    Queue-aware dispatcher that spawns ``process_single_job`` workers
    based on per-queue-key depth in the unified ``worker_jobs`` table.

    Runs every ``POLL_INTERVAL_SECONDS``:
    1. Reaps orphaned ``queue_slots`` leases and stale ``worker_jobs``
       rows so the queue can make forward progress.
    2. Scans ``worker_jobs`` for active queue keys and their
       queued/running counts.
    3. Spawns up to ``MAX_WORKERS_PER_POLL`` ``process_single_job``
       workers, budgeted per queue_key against concurrency limits.
    """
    console.print("[cyan]Queue dispatcher starting...[/cyan]")
    await configure_storage_paths()

    try:
        stale_cleared = await cleanup_stale_queue_slots()
        if stale_cleared > 0:
            console.print(f"metric=queue_lock_stale_cleared count={stale_cleared}")
            console.print(
                f"[dim]Cleared {stale_cleared} stale queue slot lock(s)[/dim]"
            )

        cleanup_counts = await cleanup_orphaned_queue_state()
        if any(cleanup_counts.values()):
            console.print(
                "metric=orphaned_queue_cleanup "
                + " ".join(f"{key}={value}" for key, value in cleanup_counts.items())
            )
            console.print(
                "[yellow]Reconciled orphaned queue state:[/yellow] "
                + ", ".join(
                    f"{key}={value}"
                    for key, value in cleanup_counts.items()
                    if value > 0
                )
            )

        queue_keys = await discover_active_worker_job_queue_keys()
        queue_counts = await get_worker_job_queue_counts(queue_keys)
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
            *(
                process_single_job.spawn.aio(queue_key=queue_key)
                for queue_key in spawn_plan
            )
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

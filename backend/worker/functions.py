import asyncio
from uuid import uuid4

import asyncpg
import modal

from modal_app import (
    MAX_WORKERS_PER_POLL,
    POLL_INTERVAL_SECONDS,
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
from oddish.config import settings
from oddish.db import close_database_connections
from oddish.workers.queue.dispatch_planner import (
    build_spawn_plan,
    discover_active_queue_keys,
    get_queue_counts,
)

from .cleanup import cleanup_orphaned_queue_state
from .queue_manager import create_single_job_queue_manager, run_single_job_without_listener
from .runtime import configure_storage_paths, console
from .slots import acquire_queue_slot, cleanup_stale_queue_slots, release_queue_slot


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
    await configure_storage_paths()

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

        qm, qm_connection = await create_single_job_queue_manager(
            queue_key=queue_key,
            worker_id=worker_id,
            queue_slot=lock_slot,
        )

        job_found = await run_single_job_without_listener(qm)
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
            await release_queue_slot(
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

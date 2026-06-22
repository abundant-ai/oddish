from oddish.config import Settings

# Worker containers run ONE job for its full duration -- agent trials can run
# for many minutes up to ~10 hours. A pooled (QueuePool) connection would be
# opened by the first session (``_prepare_trial_run``) and then held *idle* for
# the entire trial, even though the worker only touches the DB for a few ms
# every 30s (heartbeats) plus a claim and a final write. Across a large fleet
# that's hundreds of connections held idle for hours against the Supavisor
# client cap.
#
# NullPool makes every SQLAlchemy session short-lived: open a connection, run
# its quick transaction, close it. Combined with the runner's per-op asyncpg
# connections (claim / heartbeat / outcome) and slots.py's per-op connections,
# a worker holds *no* DB connection during the long Harbor run -- only the few
# workers actively writing at any instant consume a client connection. This is
# what lets the fleet scale past the naive ``workers × held_conns`` ceiling.
# Supavisor transaction mode is built for exactly this transient-connection
# pattern. The API keeps its QueuePool (see endpoints.py) because it is warm,
# long-lived, and latency-sensitive.
Settings.db_use_null_pool = True

import asyncio
import time
from uuid import uuid4

import modal

# ``configure_logfire`` runs in ``worker/__init__.py`` (so auto-tracing
# can patch our modules before they're imported); we just need the
# ``span`` helper here to wrap entry points.
from observability import span as _otel_span

from modal_app import (
    CLEANUP_INTERVAL_SECONDS,
    CLEANUP_TIMEOUT_SECONDS,
    DISPATCHER_CPU,
    DISPATCHER_MEMORY_MB,
    DISPATCHER_NONPREEMPTIBLE,
    DISPATCHER_TIMEOUT_SECONDS,
    MAX_WORKERS_PER_POLL,
    POLL_INTERVAL_SECONDS,
    RECONCILER_CPU,
    RECONCILER_MEMORY_MB,
    WORKER_BATCH_BUDGET_SECONDS,
    WORKER_BUFFER_CONTAINERS,
    WORKER_CPU,
    WORKER_MAX_CONTAINERS,
    WORKER_MEMORY_MB,
    WORKER_MIN_CONTAINERS,
    WORKER_NONPREEMPTIBLE,
    WORKER_SCALEDOWN_WINDOW_SECONDS,
    WORKER_TIMEOUT_SECONDS,
    app,
    image,
    runtime_secrets,
    worker_volumes,
)
from dashboard_owner_backfill import backfill_experiment_owners
from oddish.config import settings
from oddish.db import close_database_connections, get_session, WorkerJobKind
from oddish.workers.jobs import ensure_builtin_handlers_registered
from oddish.workers.queue.cleanup import cleanup_orphaned_queue_state
from oddish.workers.queue.concurrency_controller import (
    get_advisory_limits,
    merge_advisory_over_static,
    recompute_advisory_limits,
)
from oddish.workers.queue.slots import (
    acquire_queue_slot,
    cleanup_stale_queue_slots,
    release_queue_slot,
)
from oddish.workers.queue.runtime_status import (
    DISPATCHER_COMPONENT,
    RECONCILER_COMPONENT,
    record_queue_runtime_status,
)
from oddish.workers.queue.worker_job_dispatcher import (
    build_spawn_plan,
    discover_active_worker_job_queue_keys,
    get_worker_job_org_queue_counts,
)
from oddish.workers.queue.worker_job_single_job import (
    PostSuccessHooks,
    drain_worker_jobs,
)

from .github import notify_github_analysis, notify_github_qa, notify_github_trial
from .runtime import configure_storage_paths, console

# Register TRIAL / ANALYSIS / VERDICT handlers against the unified
# registry as soon as this module loads in a worker container. The
# dispatcher and single-job runner also call this defensively, but
# doing it here makes the startup order explicit for readers.
ensure_builtin_handlers_registered()


# Post-success hooks: fired after the worker_jobs row is in SUCCESS state.
# The QA hook refreshes the whole PR comment (per-trial classifications +
# task verdict) in one update. ``ANALYSIS`` is transitional -- it only fires
# for legacy per-trial rows draining across a deploy. Hook exceptions are
# swallowed by the runner so a GitHub API hiccup never corrupts scheduling
# state.
_POST_SUCCESS_HOOKS: PostSuccessHooks = {
    WorkerJobKind.TRIAL: notify_github_trial,
    WorkerJobKind.QA: notify_github_qa,
    WorkerJobKind.ANALYSIS: notify_github_analysis,
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
    cpu=WORKER_CPU,
    memory=WORKER_MEMORY_MB,
    nonpreemptible=WORKER_NONPREEMPTIBLE,
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
    # Resolve the Modal function-call id BEFORE opening the span so
    # it can ride as a span attribute. This is a pure in-process
    # lookup, no I/O, so it's safe to do pre-span.
    fc_id: str | None = None
    try:
        fc_id = modal.current_function_call_id()
    except Exception:
        pass

    # Open the span FIRST and run everything else inside it — the
    # ``configure_storage_paths`` + Modal handshake + console prints
    # used to fire ahead of the span and showed up as orphan top-
    # level activity on every job worker container.
    job_span = _otel_span(
        "worker.process_single_job",
        queue_key=queue_key,
        modal_function_call_id=fc_id,
    )

    worker_id = f"{queue_key}-{uuid4().hex[:12]}"
    lock_slot: int | None = None

    job_span.__enter__()
    try:
        console.print(f"[cyan]Job worker starting (queue_key={queue_key})...[/cyan]")
        if fc_id:
            console.print(f"[dim]Modal function call: {fc_id}[/dim]")
        await configure_storage_paths()

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

        jobs_processed = await drain_worker_jobs(
            queue_key,
            worker_id=worker_id,
            queue_slot=lock_slot,
            budget_seconds=WORKER_BATCH_BUDGET_SECONDS,
            modal_function_call_id=fc_id,
            post_success_hooks=_POST_SUCCESS_HOOKS,
        )
        if jobs_processed == 0:
            console.print(
                f"[dim]No job available after slot acquisition (queue_key={queue_key})[/dim]"
            )
        elif jobs_processed > 1:
            console.print(
                f"metric=worker_batch_drained queue_key={queue_key} "
                f"jobs={jobs_processed}"
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
        import sys as _sys

        try:
            job_span.__exit__(*_sys.exc_info())
        except Exception:
            pass
        console.print("[green]Job worker complete[/green]")


@app.function(
    image=image,
    volumes=worker_volumes,
    secrets=runtime_secrets,
    timeout=CLEANUP_TIMEOUT_SECONDS,
    cpu=RECONCILER_CPU,
    memory=RECONCILER_MEMORY_MB,
    min_containers=1,  # Keep one reconciler container warm.
    max_containers=1,  # Singleton-ish: never run two sweeps at once.
    schedule=modal.Period(seconds=CLEANUP_INTERVAL_SECONDS),
    nonpreemptible=DISPATCHER_NONPREEMPTIBLE,
)
async def reconcile_queue_state():
    """
    Scheduled reconciliation sweep, decoupled from dispatch.

    This used to run inline at the top of ``poll_queue`` under a 60s
    timeout. The sweep does several near-full-table passes over
    ``trials`` / ``tasks`` / ``worker_jobs`` and could (a) exceed the
    dispatcher's tight timeout -- getting SIGKILLed mid-transaction and
    leaving orphaned 'idle in transaction' locks -- and (b) deadlock
    against the live single-job workers writing the same rows. Either
    way the dispatcher's spawn step never ran, so the queue got *no*
    new workers that cycle. Splitting it out means dispatch can never
    be blocked by reconciliation, and the sweep gets a generous timeout
    so it is never killed mid-transaction.

    Each phase is wrapped defensively: a failure (e.g. a transient
    deadlock) in one phase logs and is swallowed so the remaining
    phases still run.
    """
    cycle_span = _otel_span("worker.reconcile_queue_state")
    cycle_span.__enter__()
    started = time.monotonic()
    # Accumulated for the persisted heartbeat the admin dashboard reads back.
    summary: dict[str, int] = {}
    phase_errors: list[str] = []
    try:
        console.print("[cyan]Queue reconciler starting...[/cyan]")
        await configure_storage_paths()

        try:
            stale_cleared = await cleanup_stale_queue_slots()
            summary["stale_slots_cleared"] = stale_cleared
            if stale_cleared > 0:
                console.print(f"metric=queue_lock_stale_cleared count={stale_cleared}")
                console.print(
                    f"[dim]Cleared {stale_cleared} stale queue slot lock(s)[/dim]"
                )
        except Exception as e:  # noqa: BLE001 - best-effort phase
            phase_errors.append(f"stale_slot_cleanup: {e}")
            console.print(f"[yellow]Stale slot cleanup skipped: {e}[/yellow]")

        try:
            cleanup_counts = await cleanup_orphaned_queue_state()
            summary.update({k: int(v) for k, v in cleanup_counts.items()})
            if any(cleanup_counts.values()):
                console.print(
                    "metric=orphaned_queue_cleanup "
                    + " ".join(
                        f"{key}={value}" for key, value in cleanup_counts.items()
                    )
                )
                console.print(
                    "[yellow]Reconciled orphaned queue state:[/yellow] "
                    + ", ".join(
                        f"{key}={value}"
                        for key, value in cleanup_counts.items()
                        if value > 0
                    )
                )
        except Exception as e:  # noqa: BLE001 - best-effort phase
            phase_errors.append(f"orphaned_state: {e}")
            console.print(
                f"[yellow]Orphaned-state reconciliation skipped: {e}[/yellow]"
            )

        try:
            backfill_counts = await backfill_experiment_owners()
            summary.update({k: int(v) for k, v in backfill_counts.items()})
            if any(backfill_counts.values()):
                console.print(
                    "metric=experiments_owner_backfill "
                    + " ".join(
                        f"{key}={value}" for key, value in backfill_counts.items()
                    )
                )
        except Exception as e:  # noqa: BLE001 - best-effort phase
            phase_errors.append(f"owner_backfill: {e}")
            console.print(f"[yellow]Experiment owner backfill skipped: {e}[/yellow]")

        # Recompute the self-tuning per-model concurrency advisory (default off).
        # A defensive phase like the others: a failure logs and is swallowed so
        # the rest of the reconcile sweep still runs, and the static limits stay
        # the fallback.
        if settings.dynamic_model_concurrency:
            try:
                async with get_session() as session:
                    advisory = await recompute_advisory_limits(session)
                summary["advisory_limits_updated"] = len(advisory)
            except Exception as e:  # noqa: BLE001 - best-effort phase
                phase_errors.append(f"concurrency_controller: {e}")
                console.print(f"[yellow]Concurrency controller skipped: {e}[/yellow]")

        # Persist a heartbeat the admin dashboard reads back. Keep only
        # non-zero counters so the payload stays legible.
        await record_queue_runtime_status(
            RECONCILER_COMPONENT,
            {
                "ok": not phase_errors,
                "duration_seconds": round(time.monotonic() - started, 2),
                "errors": phase_errors,
                "counts": {k: v for k, v in summary.items() if v},
            },
        )

    except OSError as e:
        console.print(
            f"[yellow]Reconciler skipped (transient network error): {e}[/yellow]"
        )
    except Exception as e:
        console.print(f"[red]Reconciler error: {e}[/red]")
        raise
    finally:
        await close_database_connections()
        import sys as _sys

        try:
            cycle_span.__exit__(*_sys.exc_info())
        except Exception:
            pass
        console.print("[green]Reconciler complete[/green]")


@app.function(
    image=image,
    volumes=worker_volumes,
    secrets=runtime_secrets,
    timeout=DISPATCHER_TIMEOUT_SECONDS,
    cpu=DISPATCHER_CPU,
    memory=DISPATCHER_MEMORY_MB,
    min_containers=1,  # Keep one dispatcher container warm.
    max_containers=1,  # Keep the scheduled dispatcher singleton-ish.
    schedule=modal.Period(seconds=POLL_INTERVAL_SECONDS),
    nonpreemptible=DISPATCHER_NONPREEMPTIBLE,
)
async def poll_queue():
    """
    Queue-aware dispatcher that spawns ``process_single_job`` workers
    based on per-queue-key depth in the unified ``worker_jobs`` table.

    Runs every ``POLL_INTERVAL_SECONDS`` and does only two things --
    discover active queue keys and spawn workers. Reconciliation
    (stale-heartbeat reap, stage advances, orphaned slot release, owner
    backfill) lives in the separate ``reconcile_queue_state`` function
    so a slow or deadlocking sweep can never block dispatch:

    1. Scans ``worker_jobs`` for active queue keys and their
       queued/running counts.
    2. Spawns up to ``MAX_WORKERS_PER_POLL`` ``process_single_job``
       workers, budgeted per queue_key against concurrency limits.
    """
    # Open the span FIRST so the storage-path setup, discovery, and
    # spawn-plan queries all nest under one named
    # ``worker.poll_queue_cycle`` parent per tick.
    cycle_span = _otel_span("worker.poll_queue_cycle")
    cycle_span.__enter__()
    try:
        console.print("[cyan]Queue dispatcher starting...[/cyan]")
        await configure_storage_paths()

        queue_keys = await discover_active_worker_job_queue_keys()
        queued_by_org_queue, running_by_queue = await get_worker_job_org_queue_counts(
            queue_keys
        )
        concurrency_limits = {
            queue_key: settings.get_model_concurrency(queue_key)
            for queue_key in queue_keys
        }
        # Single injection point for the self-tuning controller: when enabled,
        # overlay the fresh per-queue advisory limit on the static one (a stale,
        # missing, or errored advisory decays to the static value). Best-effort:
        # a read failure must never block dispatch.
        if settings.dynamic_model_concurrency:
            try:
                async with get_session() as session:
                    advisory_limits = await get_advisory_limits(session)
                concurrency_limits = merge_advisory_over_static(
                    concurrency_limits, advisory_limits
                )
            except Exception as e:  # noqa: BLE001 - advisory read is best-effort
                console.print(f"[yellow]Advisory limits unavailable: {e}[/yellow]")

        # Per-queue_key summary (aggregated across orgs) is still the
        # useful operator-facing view.
        queued_by_queue: dict[str, int] = {}
        for (_org_id, queue_key), queued in queued_by_org_queue.items():
            queued_by_queue[queue_key] = queued_by_queue.get(queue_key, 0) + queued
        for queue_key in queue_keys:
            queued = queued_by_queue.get(queue_key, 0)
            running = running_by_queue.get(queue_key, 0)
            limit = concurrency_limits.get(queue_key, 0)
            console.print(
                f"[dim]{queue_key}: queued={queued} running={running} limit={limit}[/dim]"
            )

        # Also log the per-(org, queue_key) breakdown so a lopsided
        # fair-share allocation is debuggable from the poll log without
        # digging into the DB.
        if queued_by_org_queue:
            org_buckets: dict[str, int] = {}
            for (org_id, _queue_key), queued in queued_by_org_queue.items():
                key = org_id or "<none>"
                org_buckets[key] = org_buckets.get(key, 0) + queued
            summary = ", ".join(
                f"{org}={count}" for org, count in sorted(org_buckets.items())
            )
            console.print(f"[dim]queued_by_org: {summary}[/dim]")

        console.print(f"[dim]Spawn cap per poll: {MAX_WORKERS_PER_POLL}[/dim]")

        spawn_plan = build_spawn_plan(
            queued_by_org_queue=queued_by_org_queue,
            running_by_queue=running_by_queue,
            concurrency_limits=concurrency_limits,
            max_workers=MAX_WORKERS_PER_POLL,
        )

        # Persist a heartbeat the admin dashboard reads back so operators can
        # see the spawn rate (and whether it is pinned at the per-poll cap)
        # without scraping Modal logs.
        await record_queue_runtime_status(
            DISPATCHER_COMPONENT,
            {
                "spawned": len(spawn_plan),
                "max_workers_per_poll": MAX_WORKERS_PER_POLL,
                "spawn_cap_reached": len(spawn_plan) >= MAX_WORKERS_PER_POLL,
                "active_queue_keys": len(queue_keys),
                "queued_total": sum(queued_by_queue.values()),
                "running_total": sum(running_by_queue.values()),
            },
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
        # crash the scheduled function -- the next poll in 3 minutes will retry.
        console.print(
            f"[yellow]Dispatcher skipped (transient network error): {e}[/yellow]"
        )
    except Exception as e:
        console.print(f"[red]Dispatcher error: {e}[/red]")
        raise
    finally:
        await close_database_connections()
        import sys as _sys

        try:
            cycle_span.__exit__(*_sys.exc_info())
        except Exception:
            pass
        console.print("[green]Dispatcher complete[/green]")

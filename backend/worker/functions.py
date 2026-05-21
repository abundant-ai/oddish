from oddish.config import Settings

# Worker containers process one job each; keep DB pools minimal to avoid
# exhausting connection limits when Modal bursts many containers.
Settings.db_pool_size = 1
Settings.db_pool_max_overflow = 0

import asyncio
from uuid import uuid4

import modal

from observability import span as _otel_span

from modal_app import (
    DB_PROXY_ENABLED,
    WORKER_BUFFER_CONTAINERS,
    WORKER_MAX_CONTAINERS,
    WORKER_MIN_CONTAINERS,
    WORKER_NONPREEMPTIBLE,
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
from oddish.workers.queue.slots import (
    acquire_queue_slot,
    release_queue_slot,
)
from oddish.workers.queue.worker_job_single_job import (
    PostSuccessHooks,
    run_single_worker_job,
)

from .db_proxy_service import DBProxyService, install_modal_db_proxy  # noqa: F401
from .dispatcher import dispatcher_notify, install_modal_dispatch_waker  # noqa: F401
from .github import notify_github_analysis, notify_github_trial, notify_github_verdict
from .runtime import configure_storage_paths, console

install_modal_dispatch_waker()

if DB_PROXY_ENABLED:
    install_modal_db_proxy()

ensure_builtin_handlers_registered()


_POST_SUCCESS_HOOKS: PostSuccessHooks = {
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
    memory=1024,
    nonpreemptible=WORKER_NONPREEMPTIBLE,
)
async def process_single_job(queue_key: str):
    """Process exactly ONE ``worker_jobs`` row from the unified queue."""
    fc_id: str | None = None
    try:
        fc_id = modal.current_function_call_id()
    except Exception:
        pass

    job_span = _otel_span(
        "worker.process_single_job",
        queue_key=queue_key,
        modal_function_call_id=fc_id,
    )

    worker_id = f"{queue_key}-{uuid4().hex[:12]}"
    lock_slot: int | None = None
    unmetered = settings.is_unmetered_queue_key(queue_key)

    job_span.__enter__()
    try:
        console.print(f"[cyan]Job worker starting (queue_key={queue_key})...[/cyan]")
        if fc_id:
            console.print(f"[dim]Modal function call: {fc_id}[/dim]")
        await configure_storage_paths()

        if unmetered:
            console.print(
                f"[dim]Unmetered queue (queue_key={queue_key}), "
                f"skipping queue_slot lease[/dim]"
            )
        else:
            queue_limit = settings.get_model_concurrency(queue_key)
            if queue_limit <= 0:
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
                return
            console.print(
                f"metric=queue_lock_acquired queue_key={queue_key} "
                f"slot={lock_slot + 1} limit={queue_limit}"
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
        elif unmetered:
            from oddish.workers.queue.dispatch_signal import notify_dispatch

            await notify_dispatch(queue_key)
        await close_database_connections()
        import sys as _sys

        try:
            job_span.__exit__(*_sys.exc_info())
        except Exception:
            pass
        console.print("[green]Job worker complete[/green]")

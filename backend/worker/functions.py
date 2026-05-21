from oddish.config import Settings

# Worker containers process one job each; keep DB pools minimal.
Settings.db_pool_size = 1
Settings.db_pool_max_overflow = 0

import asyncio
from uuid import uuid4

import modal

from observability import span as _otel_span

from modal_app import (
    DB_PROXY_ENABLED,
    PROVIDER_WORKER_CAPS,
    WORKER_BUFFER_CONTAINERS,
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
from .dispatcher import install_wrapper_dispatch_waker
from .github import notify_github_analysis, notify_github_trial, notify_github_verdict
from .runtime import configure_storage_paths, console

ensure_builtin_handlers_registered()

if DB_PROXY_ENABLED:
    install_modal_db_proxy()


_POST_SUCCESS_HOOKS: PostSuccessHooks = {
    WorkerJobKind.TRIAL: notify_github_trial,
    WorkerJobKind.ANALYSIS: notify_github_analysis,
    WorkerJobKind.VERDICT: notify_github_verdict,
}


async def _do_single_job(queue_key: str) -> None:
    """Inner worker body. Called by every ``process_single_job_*`` wrapper."""
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

        if not unmetered:
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

        job_found = await run_single_worker_job(
            queue_key=queue_key,
            worker_id=worker_id,
            queue_slot=lock_slot,
            modal_function_call_id=fc_id,
            post_success_hooks=_POST_SUCCESS_HOOKS,
        )
        if not job_found:
            console.print(
                f"[dim]No job available (queue_key={queue_key})[/dim]"
            )

    except asyncio.CancelledError:
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


def _make_wrapper(name: str, max_containers: int):
    """Define an ``@app.function`` wrapper with provider-specific
    ``max_containers``. Modal's per-function container cap is the
    rate limit; no application-level queue needed."""

    async def _wrapper(queue_key: str) -> None:
        await _do_single_job(queue_key)

    _wrapper.__name__ = name
    return app.function(
        image=image,
        volumes=worker_volumes,
        secrets=runtime_secrets,
        min_containers=0,
        buffer_containers=WORKER_BUFFER_CONTAINERS,
        scaledown_window=WORKER_SCALEDOWN_WINDOW_SECONDS,
        max_containers=max_containers,
        timeout=WORKER_TIMEOUT_SECONDS,
        memory=1024,
        nonpreemptible=WORKER_NONPREEMPTIBLE,
        name=name,
        serialized=True,
    )(_wrapper)


process_single_job_baseline = _make_wrapper(
    "process_single_job_baseline", PROVIDER_WORKER_CAPS["baseline"]
)
process_single_job_openai = _make_wrapper(
    "process_single_job_openai", PROVIDER_WORKER_CAPS["openai"]
)
process_single_job_claude = _make_wrapper(
    "process_single_job_claude", PROVIDER_WORKER_CAPS["claude"]
)
process_single_job_gemini = _make_wrapper(
    "process_single_job_gemini", PROVIDER_WORKER_CAPS["gemini"]
)
process_single_job_bedrock = _make_wrapper(
    "process_single_job_bedrock", PROVIDER_WORKER_CAPS["bedrock"]
)
process_single_job_azure = _make_wrapper(
    "process_single_job_azure", PROVIDER_WORKER_CAPS["azure"]
)
process_single_job = _make_wrapper(
    "process_single_job", PROVIDER_WORKER_CAPS["default"]
)


PROVIDER_WRAPPERS: dict[str, modal.Function] = {
    "baseline": process_single_job_baseline,
    "openai": process_single_job_openai,
    "claude": process_single_job_claude,
    "gemini": process_single_job_gemini,
    "bedrock": process_single_job_bedrock,
    "azure": process_single_job_azure,
    "default": process_single_job,
}


def get_wrapper_for_queue_key(queue_key: str) -> modal.Function:
    provider = settings.get_provider_for_queue_key(queue_key)
    return PROVIDER_WRAPPERS.get(provider, process_single_job)


# Install the waker AFTER wrappers exist so dispatcher.py can route.
install_wrapper_dispatch_waker(get_wrapper_for_queue_key)

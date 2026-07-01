from __future__ import annotations

from uuid import uuid4

from oddish.config import settings
from oddish.workers.queue.shared import console
from oddish.workers.queue.slots import acquire_queue_slot, release_queue_slot
from oddish.workers.queue.worker_job_dispatcher import stamp_dispatch_stage
from oddish.workers.queue.worker_job_single_job import (
    drain_worker_jobs,
)

POLL_INTERVAL_SECONDS = 2.0
DEFAULT_MAX_WORKERS_PER_CYCLE = 256

# Off-Modal worker defaults, mirroring the Modal worker (modal_app.py): a
# single worker drains its assigned queue_key for this wall-clock budget --
# a long trial blows the budget on its first job (one-per-container), short
# jobs pack many into one slot lease.
DEFAULT_BATCH_BUDGET_SECONDS = 300.0
DEFAULT_SLOT_LEASE_SECONDS = 43230  # WORKER_TIMEOUT_SECONDS (12h) + 30s


async def run_polling_worker(
    *,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    max_workers: int = DEFAULT_MAX_WORKERS_PER_CYCLE,
) -> None:
    """Run the standalone in-process worker pool via the shared dispatch loop.

    This is the self-host / ``python -m oddish.server`` compatibility entrypoint.
    The scheduling brain lives in ``oddish.dispatch.cycle``; this wrapper only
    selects the in-process fan-out backend and preserves the old polling interval
    as the dispatch loop's fallback wake.
    """
    # Lazy imports avoid a cycle during ``oddish.dispatch.cycle`` import:
    # cycle -> slots -> workers package -> queue_manager.
    from oddish.dispatch.backends.inprocess import InProcessDispatcher
    from oddish.dispatch.cycle import run_dispatch_loop

    await run_dispatch_loop(
        InProcessDispatcher(worker_id_prefix="oss"),
        max_workers=max_workers,
        concurrency_for=settings.get_model_concurrency,
        on_stage=stamp_dispatch_stage,
        fallback_interval=poll_interval,
    )


async def run_assigned_queue_worker(
    queue_key: str,
    *,
    worker_id: str | None = None,
    budget_seconds: float = DEFAULT_BATCH_BUDGET_SECONDS,
    lease_seconds: int = DEFAULT_SLOT_LEASE_SECONDS,
) -> int:
    """Drain one ``queue_key`` on a single concurrency slot, then exit.

    The host-agnostic analog of Modal's ``process_single_job``: acquire a
    ``queue_slots`` lease, ``drain_worker_jobs`` for the wall-clock budget,
    release the slot. This is the entrypoint a Docker / Kubernetes one-job
    worker container runs (``Dispatcher.spawn`` launches one per ``queue_key``).
    Returns the number of jobs processed.
    """
    worker_id = worker_id or f"{queue_key}-{uuid4().hex[:12]}"
    limit = settings.get_model_concurrency(queue_key)
    if limit <= 0:
        console.print(
            f"[dim]Queue limit is {limit} (queue_key={queue_key}), exiting[/dim]"
        )
        return 0

    slot = await acquire_queue_slot(
        queue_key=queue_key,
        limit=limit,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
    )
    if slot is None:
        console.print(
            f"[dim]No queue slots available (queue_key={queue_key}), exiting[/dim]"
        )
        return 0

    try:
        # Off-Modal workers are image-agnostic (no per-variant images), so one
        # worker per queue_key drains EVERY variant -- claim any variant
        # (harbor_variant_id=None) rather than only the "default" one, which
        # would strand jobs queued for non-default variants.
        return await drain_worker_jobs(
            queue_key,
            worker_id=worker_id,
            queue_slot=slot,
            budget_seconds=budget_seconds,
            harbor_variant_id=None,
        )
    finally:
        await release_queue_slot(
            queue_key=queue_key,
            slot=slot,
            worker_id=worker_id,
        )

from __future__ import annotations

from uuid import uuid4

from oddish.config import settings
from oddish.core.model_concurrency import (
    load_effective_model_concurrency_limit,
    load_effective_model_concurrency_limits,
)
from oddish.workers.queue.shared import console
from oddish.workers.queue.sandbox_capacity import (
    SANDBOX_CAPACITY_LEASE_SECONDS,
    acquire_sandbox_capacity_lease,
    release_sandbox_capacity_lease,
)
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
    from oddish.dispatch.cycle import (
        load_sandbox_capacity_by_lane,
        run_dispatch_loop,
    )

    await run_dispatch_loop(
        InProcessDispatcher(worker_id_prefix="oss"),
        max_workers=max_workers,
        concurrency_limits_for=load_effective_model_concurrency_limits,
        on_stage=stamp_dispatch_stage,
        capacity_by_lane=load_sandbox_capacity_by_lane,
        fallback_interval=poll_interval,
    )


async def run_assigned_queue_worker(
    queue_key: str,
    *,
    worker_id: str | None = None,
    budget_seconds: float = DEFAULT_BATCH_BUDGET_SECONDS,
    lease_seconds: int = DEFAULT_SLOT_LEASE_SECONDS,
    harbor_variant_id: str | None = None,
    execution_lane: str = "default",
) -> int:
    """Drain one ``queue_key`` on a single concurrency slot, then exit.

    The host-agnostic analog of Modal's ``process_single_job``: acquire a
    ``queue_slots`` lease, ``drain_worker_jobs`` for the wall-clock budget,
    release the slot. This is the entrypoint a Docker / Kubernetes one-job
    worker container runs (``Dispatcher.spawn`` launches one per ``queue_key``).
    Returns the number of jobs processed.
    """
    worker_id = worker_id or f"{queue_key}-{uuid4().hex[:12]}"
    if execution_lane not in {"default", "ec2_trial"}:
        raise ValueError(f"unsupported execution lane: {execution_lane!r}")
    limit = await load_effective_model_concurrency_limit(queue_key)
    if limit <= 0:
        console.print(
            f"[dim]Queue limit is {limit} (queue_key={queue_key}), exiting[/dim]"
        )
        return 0

    capacity_slot: int | None = None
    try:
        if execution_lane == "ec2_trial":
            capacity_slot = await acquire_sandbox_capacity_lease(
                provider="ec2",
                limit=settings.ec2_max_concurrent_instances,
                worker_id=worker_id,
                lease_seconds=SANDBOX_CAPACITY_LEASE_SECONDS,
            )
            if capacity_slot is None:
                console.print("[dim]No EC2 capacity slots available, exiting[/dim]")
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
            return await drain_worker_jobs(
                queue_key,
                worker_id=worker_id,
                queue_slot=slot,
                budget_seconds=budget_seconds,
                harbor_variant_id=harbor_variant_id,
                execution_lane=execution_lane,
                capacity_provider="ec2" if capacity_slot is not None else None,
                capacity_slot=capacity_slot,
            )
        finally:
            await release_queue_slot(
                queue_key=queue_key,
                slot=slot,
                worker_id=worker_id,
            )
    finally:
        if capacity_slot is not None:
            await release_sandbox_capacity_lease(
                provider="ec2", slot=capacity_slot, worker_id=worker_id
            )

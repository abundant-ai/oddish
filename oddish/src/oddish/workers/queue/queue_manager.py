from __future__ import annotations

import asyncio
from uuid import uuid4

from oddish.config import settings
from oddish.workers.queue.shared import console
from oddish.workers.queue.slots import acquire_queue_slot, release_queue_slot
from oddish.workers.queue.worker_job_dispatcher import (
    discover_active_worker_job_queue_keys,
)
from oddish.workers.queue.worker_job_single_job import (
    drain_worker_jobs,
    run_single_worker_job,
)

POLL_INTERVAL_SECONDS = 2.0

# Off-Modal worker defaults, mirroring the Modal worker (modal_app.py): a
# single worker drains its assigned queue_key for this wall-clock budget --
# a long trial blows the budget on its first job (one-per-container), short
# jobs pack many into one slot lease.
DEFAULT_BATCH_BUDGET_SECONDS = 300.0
DEFAULT_SLOT_LEASE_SECONDS = 43230.0  # WORKER_TIMEOUT_SECONDS (12h) + 30s


def _get_concurrency_limits(queue_keys: tuple[str, ...]) -> dict[str, int]:
    try:
        from oddish.server import get_queue_concurrency

        return {qk: get_queue_concurrency(qk) for qk in queue_keys}
    except Exception:
        return {qk: settings.get_model_concurrency(qk) for qk in queue_keys}


async def run_polling_worker(
    *,
    poll_interval: float = POLL_INTERVAL_SECONDS,
) -> None:
    """Simple polling worker that claims and executes jobs.

    Each queue key gets up to its concurrency limit of concurrent
    jobs. The loop polls periodically and fills capacity. Jobs come
    from the unified ``worker_jobs`` table and are routed to the
    registered handler for each row's ``kind``.
    """
    # Keyed by the effective dispatch unit ``(queue_key, harbor_variant_id)`` so
    # an override (e.g. ``ephemeral``) is claimed and run on its own lane.
    active_tasks: dict[tuple[str, str], set[asyncio.Task]] = {}

    # Importing the jobs package registers the built-in handlers as a
    # side effect.
    from oddish.workers import jobs as _jobs  # noqa: F401

    while True:
        try:
            queue_units = await discover_active_worker_job_queue_keys()
            queue_keys = tuple({qk for qk, _variant in queue_units})
            limits = _get_concurrency_limits(queue_keys)

            # Reap completed tasks across every known unit first.
            for unit in list(active_tasks):
                done = {t for t in active_tasks[unit] if t.done()}
                for t in done:
                    try:
                        t.result()
                    except Exception as exc:
                        console.print(
                            f"[red]Worker task error ({unit[0]}/{unit[1]}): {exc}[/red]"
                        )
                active_tasks[unit] -= done

            # Fill capacity, sharing each queue_key's limit across its variants.
            for unit in queue_units:
                active_tasks.setdefault(unit, set())
            for qk in queue_keys:
                units = [u for u in queue_units if u[0] == qk]
                active_for_qk = sum(len(active_tasks[u]) for u in units)
                available = max(limits.get(qk, 1) - active_for_qk, 0)
                i = 0
                while available > 0 and units:
                    unit = units[i % len(units)]
                    task = asyncio.create_task(
                        _run_job_safe(unit[0], unit[1]),
                        name=f"worker-{unit[0]}-{unit[1]}",
                    )
                    active_tasks[unit].add(task)
                    available -= 1
                    i += 1

        except Exception as exc:
            console.print(f"[red]Poll loop error: {exc}[/red]")

        await asyncio.sleep(poll_interval)


async def _run_job_safe(queue_key: str, harbor_variant_id: str = "default") -> None:
    """Claim and run one job, swallowing errors so the task set stays clean."""
    worker_id = f"oss-{queue_key}"
    try:
        await run_single_worker_job(
            queue_key,
            worker_id=worker_id,
            queue_slot=0,
            harbor_variant_id=harbor_variant_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        console.print(f"[red]Job execution error ({queue_key}): {exc}[/red]")


async def run_assigned_queue_worker(
    queue_key: str,
    *,
    worker_id: str | None = None,
    budget_seconds: float = DEFAULT_BATCH_BUDGET_SECONDS,
    lease_seconds: float = DEFAULT_SLOT_LEASE_SECONDS,
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
        return await drain_worker_jobs(
            queue_key,
            worker_id=worker_id,
            queue_slot=slot,
            budget_seconds=budget_seconds,
        )
    finally:
        await release_queue_slot(
            queue_key=queue_key,
            slot=slot,
            worker_id=worker_id,
        )

from __future__ import annotations

import asyncio

from oddish.config import settings
from oddish.workers.queue.shared import console
from oddish.workers.queue.worker_job_dispatcher import (
    discover_active_worker_job_queue_keys,
)
from oddish.workers.queue.worker_job_single_job import run_single_worker_job

POLL_INTERVAL_SECONDS = 2.0


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

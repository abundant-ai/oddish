from __future__ import annotations

import asyncio

from oddish.config import settings
from oddish.workers.queue.dispatch_planner import discover_active_queue_keys
from oddish.workers.queue.shared import console
from oddish.workers.queue.single_job import run_single_job

POLL_INTERVAL_SECONDS = 2.0


def _get_concurrency_limits(queue_keys: tuple[str, ...]) -> dict[str, int]:
    try:
        from oddish.api import get_queue_concurrency

        return {qk: get_queue_concurrency(qk) for qk in queue_keys}
    except Exception:
        return {qk: settings.get_model_concurrency(qk) for qk in queue_keys}


async def run_polling_worker(
    *,
    poll_interval: float = POLL_INTERVAL_SECONDS,
) -> None:
    """Simple polling worker that claims and executes jobs.

    Each queue key gets up to its concurrency limit of concurrent jobs.
    The loop polls periodically and fills capacity.
    """
    active_tasks: dict[str, set[asyncio.Task]] = {}

    while True:
        try:
            queue_keys = await discover_active_queue_keys()
            limits = _get_concurrency_limits(queue_keys)

            for qk in queue_keys:
                if qk not in active_tasks:
                    active_tasks[qk] = set()

                done = {t for t in active_tasks[qk] if t.done()}
                for t in done:
                    try:
                        t.result()
                    except Exception as exc:
                        console.print(f"[red]Worker task error ({qk}): {exc}[/red]")
                active_tasks[qk] -= done

                available = limits.get(qk, 1) - len(active_tasks[qk])
                for _ in range(max(available, 0)):
                    task = asyncio.create_task(
                        _run_job_safe(qk),
                        name=f"worker-{qk}",
                    )
                    active_tasks[qk].add(task)

        except Exception as exc:
            console.print(f"[red]Poll loop error: {exc}[/red]")

        await asyncio.sleep(poll_interval)


async def _run_job_safe(queue_key: str) -> None:
    """Claim and run one job, swallowing errors so the task set stays clean."""
    worker_id = f"oss-{queue_key}"
    try:
        await run_single_job(
            queue_key,
            worker_id=worker_id,
            queue_slot=0,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        console.print(f"[red]Job execution error ({queue_key}): {exc}[/red]")

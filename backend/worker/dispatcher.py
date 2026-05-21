"""Modal Function-backed dispatch waker + the dispatcher function itself.

Producer side (``set_dispatch_waker(ModalFunctionDispatchWaker())``):
when ``enqueue_worker_job`` or ``release_queue_slot`` fires, the waker
``spawn``s the ``dispatcher_notify`` Modal function and returns
immediately.

Consumer side (``dispatcher_notify``): a regular Modal function. One
invocation = one plan + spawn cycle. Modal keeps a warm container via
``min_containers=1`` so calls don't pay cold-start.
"""

from __future__ import annotations

import asyncio
import time

import modal

from oddish.workers.queue.dispatch_signal import DispatchWaker, set_dispatch_waker
from oddish.workers.queue.worker_job_dispatcher import plan_dispatch_cycle

from modal_app import MAX_WORKERS_PER_POLL, app, image, runtime_secrets

from .runtime import console


# Single-container debounce so a burst of N notifies doesn't run N
# identical plan+spawn cycles. Each notification arriving within this
# window of the last full dispatch is a no-op; the first one already
# covered the work. With max_containers=1 below, this state is shared
# across all invocations.
_DEBOUNCE_SECONDS = 0.25
_last_dispatch_at: float = 0.0


@app.function(
    image=image,
    secrets=runtime_secrets,
    min_containers=1,
    max_containers=1,
    timeout=120,
)
async def dispatcher_notify(queue_key: str = "") -> dict:
    """Called once per enqueue / slot-release event.

    Lazily import ``process_single_job`` to avoid a circular import at
    module load (functions.py imports this module too).
    """
    from .functions import process_single_job

    global _last_dispatch_at
    now = time.monotonic()
    if now - _last_dispatch_at < _DEBOUNCE_SECONDS:
        return {"debounced": True, "queue_key": queue_key}
    _last_dispatch_at = now

    spawn_plan, queued_by_queue, running_by_queue = await plan_dispatch_cycle(
        max_workers=MAX_WORKERS_PER_POLL
    )
    if not spawn_plan:
        return {
            "spawn_plan_len": 0,
            "queued_by_queue": queued_by_queue,
            "running_by_queue": running_by_queue,
        }

    console.print(
        f"[green]dispatcher_notify({queue_key!r}): spawning {len(spawn_plan)}[/green]"
    )
    results = await asyncio.gather(
        *(process_single_job.spawn.aio(queue_key=qk) for qk in spawn_plan),
        return_exceptions=True,
    )
    spawn_errors = [repr(r) for r in results if isinstance(r, BaseException)]
    if spawn_errors:
        console.print(f"[red]spawn errors: {spawn_errors[:3]}[/red]")
    return {
        "spawn_plan_len": len(spawn_plan),
        "queued_by_queue": queued_by_queue,
        "running_by_queue": running_by_queue,
        "spawn_errors": spawn_errors,
    }


class ModalFunctionDispatchWaker(DispatchWaker):
    """Fire-and-forget invocation of ``dispatcher_notify``."""

    async def wake(self, queue_key: str) -> None:
        await dispatcher_notify.spawn.aio(queue_key=queue_key)


def install_modal_dispatch_waker() -> ModalFunctionDispatchWaker:
    waker = ModalFunctionDispatchWaker()
    set_dispatch_waker(waker)
    return waker

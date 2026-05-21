"""Event-driven dispatch reactor backed by a ``modal.Queue``.

Wakes on (a) NOTIFY puts from ``enqueue_worker_job`` /
``release_queue_slot`` via ``dispatch_signal``, or (b) the exact
moment a pending retry's ``available_after`` becomes runnable. No
polling cadence; container restart hygiene is the surrounding
Modal scheduler's job.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime, timezone

import modal

from oddish.workers.queue.dispatch_signal import (
    DispatchWaker,
    set_dispatch_waker,
)
from oddish.workers.queue.worker_job_dispatcher import (
    next_retry_wake_at,
    plan_dispatch_cycle,
)

from .runtime import console


DISPATCH_QUEUE_NAME = "oddish-dispatch"
_WAKE_MARKER = "wake"


def _get_dispatch_queue() -> modal.Queue:
    return modal.Queue.from_name(DISPATCH_QUEUE_NAME, create_if_missing=True)


class ModalQueueDispatchWaker(DispatchWaker):
    def __init__(self, queue: modal.Queue | None = None) -> None:
        self._queue = queue if queue is not None else _get_dispatch_queue()

    async def wake(self, queue_key: str) -> None:
        await self._queue.put.aio(_WAKE_MARKER)


def install_modal_dispatch_waker() -> ModalQueueDispatchWaker:
    waker = ModalQueueDispatchWaker()
    set_dispatch_waker(waker)
    return waker


def _seconds_until(target: datetime) -> float:
    delta = (target - datetime.now(timezone.utc)).total_seconds()
    return delta if delta > 0 else 0.0


async def _drain_pending_wakes(queue: modal.Queue, *, max_items: int = 1024) -> int:
    drained = 0
    for _ in range(max_items):
        if await queue.get.aio(block=False) is None:
            break
        drained += 1
    return drained


async def _spawn_workers(spawner, spawn_plan: list[str]) -> None:
    if not spawn_plan:
        return
    results = await asyncio.gather(
        *(spawner(queue_key) for queue_key in spawn_plan),
        return_exceptions=True,
    )
    for queue_key, result in zip(spawn_plan, results):
        if isinstance(result, BaseException):
            console.print(
                f"[red]reactor spawn failed for queue_key={queue_key}: "
                f"{result!r}[/red]"
            )


async def _dispatch_once(spawner, *, max_workers: int, reason: str) -> None:
    spawn_plan, queued_by_queue, running_by_queue = await plan_dispatch_cycle(
        max_workers=max_workers
    )
    if not spawn_plan:
        if queued_by_queue:
            summary = ", ".join(
                f"{qk}=queued:{queued_by_queue.get(qk, 0)}/running:{running_by_queue.get(qk, 0)}"
                for qk in sorted(queued_by_queue)
            )
            console.print(
                f"[dim]reactor wake ({reason}): no spawn capacity; "
                f"depth: {summary}[/dim]"
            )
        return
    console.print(
        f"[green]reactor wake ({reason}): spawning {len(spawn_plan)} worker(s)[/green]"
    )
    await _spawn_workers(spawner, spawn_plan)


async def run_reactor(
    spawner,
    *,
    max_workers: int,
    deadline_seconds: float,
) -> None:
    install_modal_dispatch_waker()
    queue = _get_dispatch_queue()
    deadline = time.monotonic() + deadline_seconds

    # Drains anything queued during the gap between the previous
    # reactor instance exiting and this one starting.
    await _dispatch_once(spawner, max_workers=max_workers, reason="startup")

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            console.print("[dim]reactor deadline reached, exiting[/dim]")
            return

        retry_wake_at = await next_retry_wake_at()
        if retry_wake_at is not None:
            wait_for = min(remaining, _seconds_until(retry_wake_at))
        else:
            wait_for = remaining
        # modal.Queue.get rejects non-positive timeouts.
        wait_for = max(wait_for, 0.05)

        try:
            await queue.get.aio(block=True, timeout=wait_for)
            reason = "notify"
        except Exception:
            reason = "retry-timer" if retry_wake_at is not None else "deadline"

        with contextlib.suppress(Exception):
            await _drain_pending_wakes(queue)

        await _dispatch_once(spawner, max_workers=max_workers, reason=reason)

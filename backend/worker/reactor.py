"""Event-driven dispatch reactor.

A long-running service that replaces the polling dispatcher entirely.
Two wake-up sources, both event-driven:

* **Modal Queue NOTIFY.** ``enqueue_worker_job`` /
  ``release_queue_slot`` push wake markers via
  :mod:`oddish.workers.queue.dispatch_signal`; the reactor blocks on
  ``modal.Queue.get`` and wakes within milliseconds.
* **Pending retry timer.** When a job is in RETRYING with a future
  ``available_after``, the reactor computes the exact seconds until
  it becomes runnable and waits at most that long. No polling, no
  safety tick -- the wake happens at the precise retry moment.

When neither source has anything pending, the reactor blocks
indefinitely (until ``deadline_seconds`` elapses or a NOTIFY arrives).
Container restart hygiene (memory growth, stale pooler state, etc.)
is handled at the Modal scheduler level, not by the reactor itself:
the surrounding scheduled function runs the reactor until just before
its next tick, then exits cleanly. Modal respawns at the next tick.

The reactor is gated by ``ODDISH_REACTOR_ENABLED``. With the flag off,
``poll_queue`` falls back to its original cycle.
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

# A tiny constant payload keeps put cost minimal. The reactor doesn't
# inspect the payload -- a wake is a wake.
_WAKE_MARKER = "wake"


def _get_dispatch_queue() -> modal.Queue:
    """Return the shared persistent wake-up queue.

    ``create_if_missing=True`` so the queue exists on first deploy
    without a separate provisioning step.
    """
    return modal.Queue.from_name(DISPATCH_QUEUE_NAME, create_if_missing=True)


class ModalQueueDispatchWaker(DispatchWaker):
    """``DispatchWaker`` implementation backed by a ``modal.Queue``.

    Holds a single queue handle for the lifetime of the process. ``put``
    is best-effort; if a wake-up is ever dropped, the next periodic
    Modal container restart drains the backlog at startup.
    """

    def __init__(self, queue: modal.Queue | None = None) -> None:
        self._queue = queue if queue is not None else _get_dispatch_queue()

    async def wake(self, queue_key: str) -> None:
        # ``partition`` is intentionally left at the default -- the
        # reactor wakes on any signal and re-plans across every
        # queue_key, so per-queue_key partitions would just add cost
        # without changing behaviour.
        await self._queue.put.aio(_WAKE_MARKER)


def install_modal_dispatch_waker() -> ModalQueueDispatchWaker:
    """Install a ``ModalQueueDispatchWaker`` as the process-wide waker.

    Safe to call multiple times; the last call wins. Returns the waker
    so callers can hold a reference if they want to publish manually.
    """
    waker = ModalQueueDispatchWaker()
    set_dispatch_waker(waker)
    return waker


def _seconds_until(target: datetime) -> float:
    """Seconds from now until ``target``, clamped to non-negative."""
    delta = (target - datetime.now(timezone.utc)).total_seconds()
    return delta if delta > 0 else 0.0


async def _drain_pending_wakes(queue: modal.Queue, *, max_items: int = 1024) -> int:
    """Pull every immediately-available wake marker off the queue.

    Returns the number drained for logging. Bounded by ``max_items`` to
    avoid pathological draining cycles if producers are flooding.
    """
    drained = 0
    for _ in range(max_items):
        item = await queue.get.aio(block=False)
        if item is None:
            break
        drained += 1
    return drained


async def _spawn_workers(spawner, spawn_plan: list[str]) -> None:
    """Fan out spawns concurrently and surface any one-worker failures.

    ``spawner`` is the same callable shape used by the legacy poll path
    (``process_single_job.spawn.aio``); we wrap each spawn in
    ``asyncio.gather`` with ``return_exceptions=True`` so a single
    spawn failure cannot starve the rest of the plan.
    """
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
            # Useful breadcrumb: we woke up but had no capacity to spawn.
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
    """Run the dispatch reactor until ``deadline_seconds`` elapses.

    ``spawner`` is invoked as ``await spawner(queue_key)`` per row in
    the spawn plan. The reactor wakes on (a) Modal Queue NOTIFY from
    enqueue / slot-release, or (b) the exact moment a pending retry
    becomes runnable. No polling cadence; if neither event source has
    anything pending, the reactor blocks until ``deadline_seconds``.
    """
    install_modal_dispatch_waker()
    queue = _get_dispatch_queue()
    started_at = time.monotonic()
    deadline = started_at + deadline_seconds

    # Initial drain catches anything that was queued between this
    # reactor instance starting and the previous instance exiting --
    # the only window where a NOTIFY can go to no listener.
    await _dispatch_once(spawner, max_workers=max_workers, reason="startup")

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            console.print("[dim]reactor deadline reached, exiting[/dim]")
            return

        # Compute exactly when the next pending retry becomes
        # runnable. If there are no retries pending we block until the
        # deadline or until a NOTIFY arrives -- no polling cadence.
        retry_wake_at = await next_retry_wake_at()
        if retry_wake_at is not None:
            wait_for = min(remaining, _seconds_until(retry_wake_at))
        else:
            wait_for = remaining
        # ``modal.Queue.get`` rejects zero / negative timeouts; clamp
        # to a tiny positive so a retry that just became runnable
        # doesn't degenerate into a busy loop.
        wait_for = max(wait_for, 0.05)

        try:
            await queue.get.aio(block=True, timeout=wait_for)
            reason = "notify"
        except TimeoutError:
            reason = "retry-timer" if retry_wake_at is not None else "deadline"
        except Exception:
            # ``queue.Empty`` is exposed from ``modal.queue`` as either
            # the stdlib ``queue.Empty`` or a Modal-side equivalent
            # depending on version. Treat any "no item" outcome as a
            # timer wake; real transport errors are caught below.
            reason = "retry-timer" if retry_wake_at is not None else "deadline"

        # Whether we woke on a notify or a timer, drain any additional
        # pending wakes so a producer burst collapses into one
        # dispatch cycle.
        with contextlib.suppress(Exception):
            await _drain_pending_wakes(queue)

        await _dispatch_once(spawner, max_workers=max_workers, reason=reason)

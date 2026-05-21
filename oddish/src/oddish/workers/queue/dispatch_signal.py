"""Best-effort wake-up signalling for the dispatch reactor.

The library never imports Modal (or any other transport). Backend code
that wants to wake the reactor on enqueue / slot-release registers a
``DispatchWaker`` implementation via :func:`set_dispatch_waker`;
library call sites (``enqueue_worker_job``, ``release_queue_slot``)
invoke :func:`notify_dispatch`, which forwards to whatever waker was
registered, swallowing failures.

The wake-up is *advisory*. The dispatch reactor still runs a safety-net
poll on a slow cadence, so a dropped notification just costs one tick
of latency, not correctness.
"""

from __future__ import annotations

import logging
from typing import Protocol


__all__ = [
    "DispatchWaker",
    "get_dispatch_waker",
    "notify_dispatch",
    "set_dispatch_waker",
]

_log = logging.getLogger(__name__)


class DispatchWaker(Protocol):
    """Send a wake-up signal targeted at a queue_key."""

    async def wake(self, queue_key: str) -> None: ...


_waker: DispatchWaker | None = None


def set_dispatch_waker(waker: DispatchWaker | None) -> None:
    """Install (or clear) the process-wide dispatch waker."""
    global _waker
    _waker = waker


def get_dispatch_waker() -> DispatchWaker | None:
    """Return the currently-installed waker, mostly for diagnostics."""
    return _waker


async def notify_dispatch(queue_key: str) -> None:
    """Wake the dispatcher for ``queue_key`` if a waker is installed.

    Swallows every exception so an unreachable wake-up transport can
    never break enqueue or slot-release. Misses are caught by the
    reactor's safety-net poll.
    """
    waker = _waker
    if waker is None:
        return
    try:
        await waker.wake(queue_key)
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("dispatch waker failed for queue_key=%s: %r", queue_key, exc)

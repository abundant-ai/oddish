"""Best-effort dispatch wake-up signalling.

Library calls ``notify_dispatch``; the backend registers a transport
(see ``backend.worker.reactor.ModalQueueDispatchWaker``). Drops are
swallowed -- the reactor's startup drain catches anything missed.
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
    async def wake(self, queue_key: str) -> None: ...


_waker: DispatchWaker | None = None


def set_dispatch_waker(waker: DispatchWaker | None) -> None:
    global _waker
    _waker = waker


def get_dispatch_waker() -> DispatchWaker | None:
    return _waker


async def notify_dispatch(queue_key: str) -> None:
    waker = _waker
    if waker is None:
        return
    try:
        await waker.wake(queue_key)
    except Exception as exc:  # pragma: no cover
        _log.debug("dispatch waker failed for queue_key=%s: %r", queue_key, exc)

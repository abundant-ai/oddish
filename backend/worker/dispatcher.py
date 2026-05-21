"""Wrapper-routing dispatch waker.

``notify_dispatch(queue_key)`` from the library is fielded here. We
look up the correct per-provider Modal wrapper function and fire
``.spawn.aio()``. Modal handles concurrency via the wrapper's
``max_containers``; nothing else stands in the way.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from oddish.workers.queue.dispatch_signal import DispatchWaker, set_dispatch_waker


class WrapperDispatchWaker(DispatchWaker):
    def __init__(self, wrapper_for: Callable[[str], Any]) -> None:
        self._wrapper_for = wrapper_for

    async def wake(self, queue_key: str) -> None:
        wrapper = self._wrapper_for(queue_key)
        await wrapper.spawn.aio(queue_key=queue_key)


def install_wrapper_dispatch_waker(
    wrapper_for: Callable[[str], Any],
) -> WrapperDispatchWaker:
    waker = WrapperDispatchWaker(wrapper_for)
    set_dispatch_waker(waker)
    return waker

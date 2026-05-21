"""Tests for the library-side dispatch wake-up signal hook.

These tests don't touch Modal -- they verify the protocol contract
between library callers (``enqueue_worker_job``, ``release_queue_slot``)
and whatever ``DispatchWaker`` implementation is installed by the
backend layer.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.workers.queue import dispatch_signal  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_waker():
    """Reset the process-wide waker around every test."""
    dispatch_signal.set_dispatch_waker(None)
    yield
    dispatch_signal.set_dispatch_waker(None)


class _RecordingWaker:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def wake(self, queue_key: str) -> None:
        self.calls.append(queue_key)


@pytest.mark.asyncio
async def test_notify_dispatch_is_noop_when_no_waker_installed():
    # Default state: no waker. Must not raise; must not require any
    # external state. This is the path taken by tests and by deploys
    # where the reactor hook hasn't initialised yet.
    await dispatch_signal.notify_dispatch("openai/gpt-5.2")


@pytest.mark.asyncio
async def test_notify_dispatch_forwards_to_installed_waker():
    waker = _RecordingWaker()
    dispatch_signal.set_dispatch_waker(waker)

    await dispatch_signal.notify_dispatch("openai/gpt-5.2")
    await dispatch_signal.notify_dispatch("anthropic/claude-3.7-sonnet")

    assert waker.calls == ["openai/gpt-5.2", "anthropic/claude-3.7-sonnet"]


@pytest.mark.asyncio
async def test_notify_dispatch_swallows_waker_failures():
    """A failing wake-up transport must not break the enqueue path."""

    class _ExplodingWaker:
        async def wake(self, queue_key: str) -> None:
            raise RuntimeError("transport down")

    dispatch_signal.set_dispatch_waker(_ExplodingWaker())

    # The whole point: enqueue/slot-release callers don't have to know
    # the waker is broken. The reactor's safety-net poll covers it.
    await dispatch_signal.notify_dispatch("openai/gpt-5.2")


def test_get_dispatch_waker_returns_installed_waker():
    waker = _RecordingWaker()
    assert dispatch_signal.get_dispatch_waker() is None
    dispatch_signal.set_dispatch_waker(waker)
    assert dispatch_signal.get_dispatch_waker() is waker

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from oddish.dispatch.backends.fake import FakeDispatcher
from oddish.dispatch.backends.inprocess import InProcessDispatcher
from oddish.dispatch.ports import Dispatcher, WorkerHandle


def _noop_dispatchers() -> list[Dispatcher]:
    """Dispatchers that can spawn hermetically (no real infra).

    ``InProcessDispatcher`` is given a no-op job runner so ``spawn`` creates
    tasks that complete immediately without touching the database.
    """

    async def _noop_run_job(queue_key: str, **_kwargs) -> bool:
        return False

    return [FakeDispatcher(), InProcessDispatcher(run_job=_noop_run_job)]


@pytest.mark.parametrize("dispatcher", _noop_dispatchers(), ids=lambda d: d.name)
def test_name_is_nonempty_str(dispatcher: Dispatcher) -> None:
    assert isinstance(dispatcher.name, str) and dispatcher.name


@pytest.mark.parametrize("dispatcher", _noop_dispatchers(), ids=lambda d: d.name)
def test_spawn_yields_one_handle_per_plan_entry(dispatcher: Dispatcher) -> None:
    plan = ["gpt-4o", "gpt-4o", "claude"]

    async def _go() -> list[WorkerHandle]:
        return list(await dispatcher.spawn(spawn_plan=plan))

    handles = asyncio.run(_go())
    assert len(handles) == len(plan)
    assert [h.queue_key for h in handles] == plan
    for handle in handles:
        assert handle.provider == dispatcher.name
        assert isinstance(handle.serialize(), dict)


@pytest.mark.parametrize("dispatcher", _noop_dispatchers(), ids=lambda d: d.name)
def test_spawn_empty_plan_returns_no_handles(dispatcher: Dispatcher) -> None:
    async def _go() -> list[WorkerHandle]:
        return list(await dispatcher.spawn(spawn_plan=[]))

    assert asyncio.run(_go()) == []


@pytest.mark.parametrize("dispatcher", _noop_dispatchers(), ids=lambda d: d.name)
def test_check_active_yields_only_live_handles(dispatcher: Dispatcher) -> None:
    async def _go() -> list[WorkerHandle]:
        handles = list(await dispatcher.spawn(spawn_plan=["a", "b"]))
        await dispatcher.cancel(handles)
        live = [h async for h in dispatcher.check_active(handles)]
        return live

    # After cancelling every spawned worker, none remain active. (For the
    # no-op in-process runner the tasks also finish on their own, so this is
    # robust either way.)
    assert asyncio.run(_go()) == []


@pytest.mark.parametrize("dispatcher", _noop_dispatchers(), ids=lambda d: d.name)
def test_cancel_returns_count_and_is_idempotent(dispatcher: Dispatcher) -> None:
    async def _go() -> tuple[int, int]:
        handles = list(await dispatcher.spawn(spawn_plan=["x"]))
        first = await dispatcher.cancel(handles)
        second = await dispatcher.cancel(handles)
        return first, second

    first, second = asyncio.run(_go())
    assert first >= 0
    assert second == 0  # nothing left to cancel the second time


@pytest.mark.parametrize("dispatcher", _noop_dispatchers(), ids=lambda d: d.name)
def test_recover_round_trips_a_serialized_handle(dispatcher: Dispatcher) -> None:
    async def _go() -> WorkerHandle | None:
        handle = WorkerHandle(
            provider=dispatcher.name, queue_key="qk", id="worker-1", provisional=False
        )
        return await dispatcher.recover(handle.serialize())

    recovered = asyncio.run(_go())
    # recover() may be best-effort (None) for ephemeral scalers, but when it
    # returns a handle it must round-trip the identity fields.
    if recovered is not None:
        assert recovered.provider == dispatcher.name
        assert recovered.queue_key == "qk"
        assert recovered.id == "worker-1"


def test_worker_handle_serialize_deserialize_round_trip() -> None:
    handle = WorkerHandle(
        provider="modal",
        queue_key="gpt-4o",
        id="fc-123",
        provisional=False,
        metadata={"region": "us-east"},
    )
    restored = WorkerHandle.deserialize(handle.serialize())
    assert restored == handle

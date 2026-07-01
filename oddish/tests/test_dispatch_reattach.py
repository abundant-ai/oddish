from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import oddish.dispatch.cycle as cycle
from oddish.dispatch.backends.fake import FakeDispatcher
from oddish.dispatch.backends.inprocess import InProcessDispatcher
from oddish.dispatch.ports import WorkerHandle


def test_reattach_recovers_durable_handles() -> None:
    async def _fetch():
        return [("gpt-4o", "fc-1"), ("claude", "fc-2")]

    async def _go():
        return await cycle.reattach_in_flight_workers(FakeDispatcher(), _fetch=_fetch)

    recovered = asyncio.run(_go())
    # FakeDispatcher.recover round-trips the serialized handle.
    assert {h.id for h in recovered} == {"fc-1", "fc-2"}
    assert all(h.provider == "fake" for h in recovered)


def test_reattach_empty_when_no_running_handles() -> None:
    async def _fetch():
        return []

    assert (
        asyncio.run(cycle.reattach_in_flight_workers(FakeDispatcher(), _fetch=_fetch))
        == []
    )


def test_reattach_drops_handles_a_backend_cannot_recover() -> None:
    # In-process handles are not durable across a restart -> recover() returns
    # None, so nothing is reattached (lease expiry + re-claim handles it).
    async def _fetch():
        return [("q", "inproc-worker-1")]

    recovered = asyncio.run(
        cycle.reattach_in_flight_workers(InProcessDispatcher(), _fetch=_fetch)
    )
    assert recovered == []


def test_reattach_builds_handles_with_dispatcher_provider() -> None:
    seen: list[WorkerHandle] = []

    class _RecordingDispatcher(FakeDispatcher):
        async def recover(self, serialized):
            handle = WorkerHandle.deserialize(serialized)
            seen.append(handle)
            return handle

    async def _fetch():
        return [("q", "h1")]

    asyncio.run(
        cycle.reattach_in_flight_workers(_RecordingDispatcher(), _fetch=_fetch)
    )
    assert seen[0].provider == "fake"
    assert seen[0].queue_key == "q"
    assert seen[0].id == "h1"
    assert seen[0].provisional is False


def test_run_dispatch_loop_reattaches_on_startup(monkeypatch) -> None:
    called: dict[str, object] = {}

    async def _fake_reattach(dispatcher, **_kwargs):
        called["provider"] = dispatcher.name
        return [WorkerHandle(provider=dispatcher.name, queue_key="q", id="h")]

    monkeypatch.setattr(cycle, "reattach_in_flight_workers", _fake_reattach)

    async def _go():
        # _stop True from the first check -> reattach runs, zero cycles.
        await cycle.run_dispatch_loop(
            FakeDispatcher(),
            max_workers=1,
            concurrency_for=lambda qk: 1,
            _stop=lambda: True,
        )

    asyncio.run(_go())
    assert called["provider"] == "fake"

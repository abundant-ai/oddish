from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.dispatch import cycle
from oddish.dispatch.backends import inprocess
from oddish.dispatch.backends.inprocess import InProcessDispatcher
from oddish.workers.queue import queue_manager


def test_run_polling_worker_delegates_to_shared_dispatch_loop(monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def fake_concurrency(queue_keys):
        seen["queue_keys"] = queue_keys
        return {queue_key: 7 for queue_key in queue_keys}

    async def fake_dispatch_loop(
        dispatcher,
        *,
        max_workers,
        concurrency_limits_for,
        on_stage,
        capacity_by_lane,
        fallback_interval,
    ) -> None:
        seen["dispatcher"] = dispatcher
        seen["max_workers"] = max_workers
        seen["on_stage"] = on_stage
        seen["capacity_by_lane"] = capacity_by_lane
        seen["fallback_interval"] = fallback_interval
        seen["limit"] = (await concurrency_limits_for(("openai/gpt-test",)))[
            "openai/gpt-test"
        ]

    monkeypatch.setattr(
        queue_manager, "load_effective_model_concurrency_limits", fake_concurrency
    )
    monkeypatch.setattr(cycle, "run_dispatch_loop", fake_dispatch_loop)

    asyncio.run(queue_manager.run_polling_worker(poll_interval=3.5, max_workers=11))

    assert seen["dispatcher"].__class__.__name__ == "InProcessDispatcher"
    assert seen["dispatcher"].name == "inprocess"
    assert seen["max_workers"] == 11
    assert seen["fallback_interval"] == 3.5
    assert seen["on_stage"] is queue_manager.stamp_dispatch_stage
    assert seen["capacity_by_lane"] is cycle.load_sandbox_capacity_by_lane
    assert seen["queue_keys"] == ("openai/gpt-test",)
    assert seen["limit"] == 7


def test_run_polling_worker_awaits_spawned_workers_during_shutdown(monkeypatch) -> None:
    worker_started = asyncio.Event()
    worker_cancelled = asyncio.Event()
    dispatcher: InProcessDispatcher

    async def run_job(_queue_key, **_kwargs):
        worker_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            worker_cancelled.set()
            raise

    async def acquire_slot(**_kwargs):
        return 0

    async def release_slot(**_kwargs):
        return None

    dispatcher = InProcessDispatcher(
        run_job=run_job,
        acquire_slot=acquire_slot,
        release_slot=release_slot,
    )

    async def load_limit(_queue_key):
        return 1

    async def run_loop(active_dispatcher, **_kwargs):
        assert active_dispatcher is dispatcher
        await active_dispatcher.spawn(spawn_plan=["openai/gpt-test"])
        await worker_started.wait()
        await asyncio.Future()

    monkeypatch.setattr(inprocess, "InProcessDispatcher", lambda **_kwargs: dispatcher)
    monkeypatch.setattr(
        "oddish.core.model_concurrency.load_effective_model_concurrency_limit",
        load_limit,
    )
    monkeypatch.setattr(cycle, "run_dispatch_loop", run_loop)

    async def run_and_stop() -> None:
        polling_task = asyncio.create_task(queue_manager.run_polling_worker())
        await worker_started.wait()
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass

        assert worker_cancelled.is_set()
        assert dispatcher._tasks == {}

    asyncio.run(run_and_stop())

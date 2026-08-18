from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.dispatch import cycle
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

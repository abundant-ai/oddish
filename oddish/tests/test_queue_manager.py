from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.dispatch import cycle
from oddish.workers.queue import queue_manager


def test_run_polling_worker_delegates_to_shared_dispatch_loop(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_concurrency(_settings, queue_key: str) -> int:
        seen["queue_key"] = queue_key
        return 7

    async def fake_dispatch_loop(
        dispatcher,
        *,
        max_workers,
        concurrency_for,
        on_stage,
        fallback_interval,
    ) -> None:
        seen["dispatcher"] = dispatcher
        seen["max_workers"] = max_workers
        seen["on_stage"] = on_stage
        seen["fallback_interval"] = fallback_interval
        seen["limit"] = concurrency_for("openai/gpt-test")

    monkeypatch.setattr(
        type(queue_manager.settings), "get_model_concurrency", fake_concurrency
    )
    monkeypatch.setattr(cycle, "run_dispatch_loop", fake_dispatch_loop)

    asyncio.run(queue_manager.run_polling_worker(poll_interval=3.5, max_workers=11))

    assert seen["dispatcher"].__class__.__name__ == "InProcessDispatcher"
    assert seen["dispatcher"].name == "inprocess"
    assert seen["max_workers"] == 11
    assert seen["fallback_interval"] == 3.5
    assert seen["on_stage"] is queue_manager.stamp_dispatch_stage
    assert seen["queue_key"] == "openai/gpt-test"
    assert seen["limit"] == 7

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import oddish.dispatch.cycle as cycle
from oddish.workers.jobs.enqueue import _wake_dispatcher


def test_enqueue_wake_signals_the_dispatch_trigger() -> None:
    # Reset the process-wide trigger so this test is order-independent.
    cycle._TRIGGER = None

    async def _go() -> str:
        trigger = cycle.get_dispatch_trigger(fallback_interval=30.0)
        _wake_dispatcher()  # what enqueue_worker_job calls after flush
        return await trigger.wait()

    assert asyncio.run(_go()) == "signal"


def test_wake_is_a_safe_noop_when_no_loop_registered() -> None:
    cycle._TRIGGER = None
    # No trigger created -> signalling must not raise and must not create one.
    _wake_dispatcher()
    assert cycle._TRIGGER is None

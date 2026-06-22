from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.dispatch.backends.fake import FakeDispatcher
from oddish.dispatch.cycle import compute_why_waiting, run_dispatch_cycle


def test_why_waiting_over_cap_reason() -> None:
    why = compute_why_waiting(
        queued_by_queue={"busy": 4},
        running_by_queue={"busy": 2},
        concurrency_limits={"busy": 2},
        spawned_keys=set(),
        max_workers=10,
    )
    assert "slot" in why["busy"].lower()


def test_why_waiting_cap_reached_reason() -> None:
    why = compute_why_waiting(
        queued_by_queue={"q": 5},
        running_by_queue={"q": 0},
        concurrency_limits={"q": 5},
        spawned_keys=set(),  # had capacity but spawn budget exhausted
        max_workers=3,
    )
    assert "cap" in why["q"].lower()


def test_why_waiting_skips_spawned_and_empty() -> None:
    why = compute_why_waiting(
        queued_by_queue={"spawned": 2, "empty": 0},
        running_by_queue={},
        concurrency_limits={"spawned": 5, "empty": 5},
        spawned_keys={"spawned"},
        max_workers=10,
    )
    assert why == {}


def test_why_waiting_preserves_base_reasons() -> None:
    why = compute_why_waiting(
        queued_by_queue={"gpu": 1},
        running_by_queue={},
        concurrency_limits={"gpu": 5},
        spawned_keys=set(),
        max_workers=10,
        base_reasons={"gpu": "capability-rejected: no GPU"},
    )
    # An admission rejection is not overwritten by a capacity reason.
    assert why["gpu"] == "capability-rejected: no GPU"


def test_run_dispatch_cycle_still_computes_reasons_via_helper() -> None:
    # Regression: the cycle's why_waiting behavior is unchanged after extraction.
    async def _go():
        return await run_dispatch_cycle(
            FakeDispatcher(),
            max_workers=10,
            concurrency_for=lambda qk: 2,
            _discover=lambda: _aval(("busy",)),
            _counts=lambda keys: _aval(({(None, "busy"): 4}, {"busy": 2})),
        )

    result = asyncio.run(_go())
    assert "busy" in result.why_waiting
    assert "slot" in result.why_waiting["busy"].lower()


def test_on_stage_failure_never_breaks_dispatch() -> None:
    # Telemetry must not break the live dispatcher: a raising on_stage is
    # swallowed and spawning still succeeds.
    dispatcher = FakeDispatcher()

    async def _boom(spawned, why):
        raise RuntimeError("stamp DB down")

    async def _go():
        return await run_dispatch_cycle(
            dispatcher,
            max_workers=10,
            concurrency_for=lambda qk: 5,
            on_stage=_boom,
            _discover=lambda: _aval(("q",)),
            _counts=lambda keys: _aval(({(None, "q"): 1}, {"q": 0})),
        )

    result = asyncio.run(_go())
    assert [h.queue_key for h in result.handles] == ["q"]  # spawn succeeded
    assert [h.queue_key for h in dispatcher.spawned] == ["q"]


async def _aval(value):
    return value

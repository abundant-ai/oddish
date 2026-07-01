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


def test_why_waiting_skips_fully_served_and_empty() -> None:
    # "served" got a worker for each of its 2 queued rows; "empty" has none.
    why = compute_why_waiting(
        queued_by_queue={"served": 2, "empty": 0},
        running_by_queue={},
        concurrency_limits={"served": 5, "empty": 5},
        spawned_keys=["served", "served"],
        max_workers=10,
    )
    assert why == {}


def test_why_waiting_partial_serve_still_names_remaining() -> None:
    # 5 queued but only 2 workers spawned this cycle -> the 3 unserved rows must
    # still get a reason (multiplicity-aware, not just "spawned at all -> skip").
    why = compute_why_waiting(
        queued_by_queue={"m": 5},
        running_by_queue={"m": 0},
        concurrency_limits={"m": 5},
        spawned_keys=["m", "m"],
        max_workers=2,
    )
    assert "m" in why
    assert "cap" in why["m"].lower()  # per-poll spawn budget exhausted


def test_why_waiting_fully_served_by_multiplicity_is_skipped() -> None:
    # 3 queued, 3 workers spawned -> fully served, no waiting reason.
    why = compute_why_waiting(
        queued_by_queue={"m": 3},
        running_by_queue={},
        concurrency_limits={"m": 5},
        spawned_keys=["m", "m", "m"],
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
            _discover=lambda: _aval((("busy", "default"),)),
            _counts=lambda keys: _aval(
                ({(None, "busy", "default"): 4}, {("busy", "default"): 2})
            ),
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
            _discover=lambda: _aval((("q", "default"),)),
            _counts=lambda keys: _aval(
                ({(None, "q", "default"): 1}, {("q", "default"): 0})
            ),
        )

    result = asyncio.run(_go())
    assert [h.queue_key for h in result.handles] == ["q"]  # spawn succeeded
    assert [h.queue_key for h in dispatcher.spawned] == ["q"]


async def _aval(value):
    return value

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.dispatch.backends.fake import FakeDispatcher
from oddish.dispatch.cycle import (
    Admission,
    DispatchTrigger,
    admit_all,
    admit_spawn_plan,
    get_dispatch_trigger,
    run_dispatch_cycle,
    signal_dispatch,
)


# --------------------------------------------------------------------------
# Pre-spawn admission gate
# --------------------------------------------------------------------------
def test_admit_all_admits_every_queue_key() -> None:
    plan = ["a", "b", "a"]
    admitted, rejected = admit_spawn_plan(plan, admit_all)
    assert admitted == plan
    assert rejected == {}


def test_admit_spawn_plan_filters_rejected_with_reason() -> None:
    def admit(queue_key: str) -> Admission:
        if queue_key == "blocked":
            return Admission(admitted=False, reason="capability-rejected: no GPU")
        return Admission(admitted=True, reason="")

    admitted, rejected = admit_spawn_plan(["ok", "blocked", "ok"], admit)
    assert admitted == ["ok", "ok"]
    assert rejected == {"blocked": "capability-rejected: no GPU"}


# --------------------------------------------------------------------------
# Event-driven trigger (poll demoted to fallback)
# --------------------------------------------------------------------------
def test_trigger_returns_signal_when_signalled() -> None:
    trigger = DispatchTrigger(fallback_interval=30.0)

    async def _go() -> str:
        trigger.signal()
        return await trigger.wait()

    assert asyncio.run(_go()) == "signal"


def test_trigger_returns_fallback_on_timeout() -> None:
    trigger = DispatchTrigger(fallback_interval=0.01)

    async def _go() -> str:
        return await trigger.wait()

    assert asyncio.run(_go()) == "fallback"


def test_signal_dispatch_wakes_the_module_singleton() -> None:
    async def _go() -> str:
        trigger = get_dispatch_trigger(fallback_interval=30.0)
        signal_dispatch()
        return await trigger.wait()

    assert asyncio.run(_go()) == "signal"


# --------------------------------------------------------------------------
# Host-agnostic dispatch cycle
# --------------------------------------------------------------------------
def _fake_discover(keys):
    async def _discover():
        return tuple(keys)

    return _discover


def _fake_counts(queued_by_org_queue, running_by_queue):
    async def _counts(queue_keys):
        return queued_by_org_queue, running_by_queue

    return _counts


def test_run_dispatch_cycle_spawns_admitted_plan() -> None:
    dispatcher = FakeDispatcher()

    async def _go():
        return await run_dispatch_cycle(
            dispatcher,
            max_workers=10,
            concurrency_for=lambda qk: 5,
            _discover=_fake_discover(["gpt-4o"]),
            _counts=_fake_counts({(None, "gpt-4o"): 3}, {"gpt-4o": 0}),
        )

    result = asyncio.run(_go())
    assert result.spawn_plan == ["gpt-4o", "gpt-4o", "gpt-4o"]
    assert result.admitted == ["gpt-4o", "gpt-4o", "gpt-4o"]
    assert [h.queue_key for h in result.handles] == result.admitted
    assert [h.queue_key for h in dispatcher.spawned] == result.admitted


def test_run_dispatch_cycle_records_why_waiting_for_over_cap_queue() -> None:
    dispatcher = FakeDispatcher()

    async def _go():
        # 4 queued but limit 2 with 2 already running -> no capacity.
        return await run_dispatch_cycle(
            dispatcher,
            max_workers=10,
            concurrency_for=lambda qk: 2,
            _discover=_fake_discover(["busy"]),
            _counts=_fake_counts({(None, "busy"): 4}, {"busy": 2}),
        )

    result = asyncio.run(_go())
    assert result.spawn_plan == []
    assert dispatcher.spawned == []
    assert "busy" in result.why_waiting
    assert "slot" in result.why_waiting["busy"].lower()


def test_run_dispatch_cycle_records_admission_rejection_reason() -> None:
    dispatcher = FakeDispatcher()

    def admit(queue_key: str) -> Admission:
        return Admission(admitted=False, reason="image-ref unresolvable")

    async def _go():
        return await run_dispatch_cycle(
            dispatcher,
            max_workers=10,
            concurrency_for=lambda qk: 5,
            admit=admit,
            _discover=_fake_discover(["gpu-task"]),
            _counts=_fake_counts({(None, "gpu-task"): 1}, {"gpu-task": 0}),
        )

    result = asyncio.run(_go())
    assert dispatcher.spawned == []
    assert result.why_waiting["gpu-task"] == "image-ref unresolvable"

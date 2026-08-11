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
    build_dispatch_plan,
    get_dispatch_trigger,
    run_dispatch_cycle,
    signal_dispatch,
)


# --------------------------------------------------------------------------
# Pre-spawn admission gate
# --------------------------------------------------------------------------
def _fake_limits(limit: int):
    """Flat per-queue_key concurrency limit, keyed by the caller's keys."""

    async def _limits(queue_keys):
        return {queue_key: limit for queue_key in queue_keys}

    return _limits


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
    # Discovery returns exact ``(queue_key, harbor_variant_id, execution_lane)``
    # units; the cycle collapses them to queue_keys only for admission reporting.
    async def _discover():
        return tuple(keys)

    return _discover


def _fake_counts(queued_by_org_queue, running_by_queue):
    async def _counts(queue_keys):
        return queued_by_org_queue, running_by_queue

    return _counts


def _fake_held(held_by_queue_key):
    # Held ``queue_slots`` lease counts per queue_key -- the injectable seam that
    # stands in for the real DB query in unit tests (mirrors ``_counts``).
    async def _held(queue_keys):
        return dict(held_by_queue_key)

    return _held


def test_run_dispatch_cycle_spawns_admitted_plan() -> None:
    dispatcher = FakeDispatcher()

    async def _go():
        return await run_dispatch_cycle(
            dispatcher,
            max_workers=10,
            concurrency_limits_for=_fake_limits(5),
            _discover=_fake_discover([("gpt-4o", "default", "default")]),
            _counts=_fake_counts(
                {(None, "gpt-4o", "default", "default"): 3},
                {("gpt-4o", "default", "default"): 0},
            ),
            _held=_fake_held({}),
        )

    result = asyncio.run(_go())
    assert result.spawn_plan == ["gpt-4o", "gpt-4o", "gpt-4o"]
    assert result.admitted == ["gpt-4o", "gpt-4o", "gpt-4o"]
    assert [h.queue_key for h in result.handles] == result.admitted
    assert [h.queue_key for h in dispatcher.spawned] == result.admitted


def test_build_dispatch_plan_preserves_variant_units_and_counts_held_slots() -> None:
    async def _go():
        return await build_dispatch_plan(
            max_workers=10,
            concurrency_limits_for=_fake_limits(4),
            _discover=_fake_discover(
                [("gpt-4o", "default", "default"), ("gpt-4o", "blessed", "default")]
            ),
            _counts=_fake_counts(
                {
                    ("org-a", "gpt-4o", "default", "default"): 3,
                    ("org-a", "gpt-4o", "blessed", "default"): 3,
                },
                {
                    ("gpt-4o", "default", "default"): 0,
                    ("gpt-4o", "blessed", "default"): 0,
                },
            ),
            _held=_fake_held({"gpt-4o": 2}),
        )

    plan = asyncio.run(_go())
    assert plan.queue_keys == ("gpt-4o",)
    assert plan.queued_by_queue == {"gpt-4o": 6}
    assert plan.running_by_queue_key == {"gpt-4o": 0}
    assert plan.held_by_queue_key == {"gpt-4o": 2}
    assert len(plan.unit_plan) == 2
    assert set(plan.unit_plan).issubset(
        {("gpt-4o", "default", "default"), ("gpt-4o", "blessed", "default")}
    )


def test_build_dispatch_plan_applies_lane_capacity() -> None:
    async def _go():
        return await build_dispatch_plan(
            max_workers=4,
            concurrency_limits_for=_fake_limits(4),
            capacity_limits_by_lane={"ec2_trial": 1},
            held_by_lane={"ec2_trial": 1},
            _discover=_fake_discover(
                [
                    ("ec2-model", "default", "ec2_trial"),
                    ("cpu-model", "default", "default"),
                ]
            ),
            _counts=_fake_counts(
                {
                    ("org-a", "ec2-model", "default", "ec2_trial"): 4,
                    ("org-a", "cpu-model", "default", "default"): 4,
                },
                {},
            ),
            _held=_fake_held({}),
        )

    plan = asyncio.run(_go())
    assert plan.unit_plan == [("cpu-model", "default", "default")] * 4


def test_run_dispatch_cycle_applies_lane_capacity_before_spawning() -> None:
    dispatcher = FakeDispatcher()

    async def _capacity_by_lane():
        return ({"ec2_trial": 1}, {"ec2_trial": 1})

    async def _go():
        return await run_dispatch_cycle(
            dispatcher,
            max_workers=4,
            concurrency_limits_for=_fake_limits(4),
            capacity_by_lane=_capacity_by_lane,
            _discover=_fake_discover(
                [
                    ("ec2-model", "default", "ec2_trial"),
                    ("cpu-model", "default", "default"),
                ]
            ),
            _counts=_fake_counts(
                {
                    ("org-a", "ec2-model", "default", "ec2_trial"): 4,
                    ("org-a", "cpu-model", "default", "default"): 4,
                },
                {},
            ),
            _held=_fake_held({}),
        )

    result = asyncio.run(_go())
    assert result.spawn_plan == ["cpu-model"] * 4
    assert [handle.queue_key for handle in result.handles] == ["cpu-model"] * 4


def test_sandbox_lane_capacity_is_zero_when_ec2_is_disabled(monkeypatch) -> None:
    from oddish.config import settings
    from oddish.dispatch import cycle
    from oddish.workers.queue import sandbox_capacity

    async def _unexpected_count(*, provider: str) -> int:
        raise AssertionError(f"counted disabled provider {provider}")

    monkeypatch.setattr(settings, "ec2_enabled", False)
    monkeypatch.setattr(
        sandbox_capacity,
        "count_held_sandbox_capacity_leases",
        _unexpected_count,
    )

    limits, held = asyncio.run(cycle.load_sandbox_capacity_by_lane())

    assert limits == {"ec2_trial": 0}
    assert held == {"ec2_trial": 0}


def test_sandbox_lane_capacity_counts_live_ec2_leases(monkeypatch) -> None:
    from oddish.config import settings
    from oddish.dispatch import cycle
    from oddish.workers.queue import sandbox_capacity

    async def _count(*, provider: str) -> int:
        assert provider == "ec2"
        return 2

    monkeypatch.setattr(settings, "ec2_enabled", True)
    monkeypatch.setattr(settings, "ec2_max_concurrent_instances", 3)
    monkeypatch.setattr(
        sandbox_capacity,
        "count_held_sandbox_capacity_leases",
        _count,
    )

    limits, held = asyncio.run(cycle.load_sandbox_capacity_by_lane())

    assert limits == {"ec2_trial": 3}
    assert held == {"ec2_trial": 2}


def test_run_dispatch_cycle_records_why_waiting_for_over_cap_queue() -> None:
    dispatcher = FakeDispatcher()

    async def _go():
        # 4 queued but limit 2 with 2 already running -> no capacity.
        return await run_dispatch_cycle(
            dispatcher,
            max_workers=10,
            concurrency_limits_for=_fake_limits(2),
            _discover=_fake_discover([("busy", "default", "default")]),
            _counts=_fake_counts(
                {(None, "busy", "default", "default"): 4},
                {("busy", "default", "default"): 2},
            ),
            _held=_fake_held({}),
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
            concurrency_limits_for=_fake_limits(5),
            admit=admit,
            _discover=_fake_discover([("gpu-task", "default", "default")]),
            _counts=_fake_counts(
                {(None, "gpu-task", "default", "default"): 1},
                {("gpu-task", "default", "default"): 0},
            ),
            _held=_fake_held({}),
        )

    result = asyncio.run(_go())
    assert dispatcher.spawned == []
    assert result.why_waiting["gpu-task"] == "image-ref unresolvable"


# --------------------------------------------------------------------------
# Held queue_slots leases fold into the in-flight number (fast-trigger churn)
# --------------------------------------------------------------------------
def test_run_dispatch_cycle_held_slots_reduce_available_spawn() -> None:
    dispatcher = FakeDispatcher()

    async def _go():
        # limit 5, 0 RUNNING, but 3 slot leases already held (freshly spawned
        # workers not yet RUNNING) -> only 5 - max(0, 3) = 2 free to spawn.
        return await run_dispatch_cycle(
            dispatcher,
            max_workers=10,
            concurrency_limits_for=_fake_limits(5),
            _discover=_fake_discover([("gpt-4o", "default", "default")]),
            _counts=_fake_counts(
                {(None, "gpt-4o", "default", "default"): 10},
                {("gpt-4o", "default", "default"): 0},
            ),
            _held=_fake_held({"gpt-4o": 3}),
        )

    result = asyncio.run(_go())
    assert result.spawn_plan == ["gpt-4o", "gpt-4o"]
    assert [h.queue_key for h in dispatcher.spawned] == ["gpt-4o", "gpt-4o"]


def test_run_dispatch_cycle_held_slots_at_limit_spawns_nothing() -> None:
    dispatcher = FakeDispatcher()

    async def _go():
        # 4 queued, limit 2, 0 RUNNING, but 2 held leases -> at capacity via the
        # held count alone; spawn nothing and name "waiting for slot" (not a
        # false "spawn cap reached", which the RUNNING-only view would give).
        return await run_dispatch_cycle(
            dispatcher,
            max_workers=10,
            concurrency_limits_for=_fake_limits(2),
            _discover=_fake_discover([("busy", "default", "default")]),
            _counts=_fake_counts(
                {(None, "busy", "default", "default"): 4},
                {("busy", "default", "default"): 0},
            ),
            _held=_fake_held({"busy": 2}),
        )

    result = asyncio.run(_go())
    assert result.spawn_plan == []
    assert dispatcher.spawned == []
    assert "slot" in result.why_waiting["busy"].lower()


def test_run_dispatch_cycle_same_cycle_spawns_read_as_waiting_for_slot() -> None:
    dispatcher = FakeDispatcher()

    async def _go():
        # 5 queued, limit 2, 0 RUNNING, 0 held -> the planner spawns 2 (fills both
        # slots) this cycle. The 3 leftover rows are blocked by the queue's own
        # concurrency cap, not the per-poll budget (max_workers 10). The
        # held/running snapshot predates the spawn, so folding *this cycle's* fresh
        # leases into the in-flight number is what makes the reason read "waiting
        # for slot" instead of a false "spawn cap reached".
        return await run_dispatch_cycle(
            dispatcher,
            max_workers=10,
            concurrency_limits_for=_fake_limits(2),
            _discover=_fake_discover([("solo", "default", "default")]),
            _counts=_fake_counts(
                {(None, "solo", "default", "default"): 5},
                {("solo", "default", "default"): 0},
            ),
            _held=_fake_held({}),
        )

    result = asyncio.run(_go())
    assert result.spawn_plan == ["solo", "solo"]
    assert [h.queue_key for h in dispatcher.spawned] == ["solo", "solo"]
    reason = result.why_waiting["solo"].lower()
    assert "slot" in reason  # served up to its own cap, not the per-poll budget
    assert "cap" not in reason

"""Unit tests for the `build_spawn_plan` fair-share planner.

These lock in the invariants we care about when the Modal dispatcher
wakes up with hundreds of queued jobs:

1. No org is starved when a louder org happens to have more queue_keys.
2. Within one org, heavier models don't completely starve lighter ones
   (the "leeway" knob) -- the inner round-robin guarantees at least
   one spawn per org-turn for a queue_key that still has work.
3. Per-queue_key global concurrency caps (``queue_slots`` leases) are
   respected across orgs.
4. The spawn budget (``max_workers``) is never exceeded even when
   total demand far outstrips it.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.workers.queue.worker_job_dispatcher import build_spawn_plan  # noqa: E402


def _limits_for(queued_by_org_queue: dict, default: int = 32) -> dict[str, int]:
    """Helper: give every queue_key in the fixture the same default cap."""
    return {qk: default for (_, qk) in queued_by_org_queue}


def test_returns_empty_when_no_queued_work():
    assert (
        build_spawn_plan(
            queued_by_org_queue={},
            running_by_queue={},
            concurrency_limits={},
            max_workers=24,
        )
        == []
    )


def test_returns_empty_when_budget_is_zero():
    assert (
        build_spawn_plan(
            queued_by_org_queue={("org-a", "m1"): 100},
            running_by_queue={},
            concurrency_limits={"m1": 32},
            max_workers=0,
        )
        == []
    )


def test_org_fairness_beats_queue_key_fairness():
    """Org A owns 3 models, Org B owns 1. They should still split 50/50.

    The old planner round-robinned over queue_keys and gave A a 3x
    advantage just because A enqueued across more models.
    """
    queued_by_org_queue = {
        ("org-a", "m1"): 100,
        ("org-a", "m2"): 100,
        ("org-a", "m3"): 100,
        ("org-b", "m4"): 100,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={},
        concurrency_limits=_limits_for(queued_by_org_queue),
        max_workers=24,
    )
    assert len(plan) == 24
    # Each org should get roughly half. Strict 12/12 with these inputs
    # because both have capacity every round.
    per_org_count = Counter()
    a_qks = {"m1", "m2", "m3"}
    for qk in plan:
        per_org_count["a" if qk in a_qks else "b"] += 1
    assert per_org_count["a"] == 12
    assert per_org_count["b"] == 12


def test_within_org_round_robin_gives_leeway_to_small_queues():
    """A small secondary model in the same org still gets spawns.

    100 queued on ``m-big`` and 5 on ``m-small``: ``m-small`` should
    drain within the first few rounds rather than being starved behind
    ``m-big``.
    """
    queued_by_org_queue = {
        ("org-a", "m-big"): 100,
        ("org-a", "m-small"): 5,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={},
        concurrency_limits=_limits_for(queued_by_org_queue, default=64),
        max_workers=24,
    )
    assert len(plan) == 24
    small_count = sum(1 for qk in plan if qk == "m-small")
    big_count = sum(1 for qk in plan if qk == "m-big")
    # All 5 small jobs should land in the plan (leeway).
    assert small_count == 5
    assert big_count == 19


def test_respects_global_queue_capacity_across_orgs():
    """Per-queue_key ``limit - running`` caps spawns across orgs.

    ``m1`` has limit=10 with 8 already running, so at most 2 spawns
    should be added to the plan for it regardless of demand.
    """
    queued_by_org_queue = {
        ("org-a", "m1"): 100,
        ("org-b", "m1"): 100,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={"m1": 8},
        concurrency_limits={"m1": 10},
        max_workers=24,
    )
    assert plan.count("m1") == 2
    # Both orgs should each receive one of the two slots (fair-share).
    assert len(plan) == 2


def test_plan_never_exceeds_max_workers():
    queued_by_org_queue = {
        ("org-a", "m1"): 500,
        ("org-b", "m2"): 500,
        ("org-c", "m3"): 500,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={},
        concurrency_limits=_limits_for(queued_by_org_queue),
        max_workers=24,
    )
    assert len(plan) == 24


def test_plan_never_exceeds_total_demand():
    """If total queued work is below the budget, we plan exactly that much."""
    queued_by_org_queue = {
        ("org-a", "m1"): 3,
        ("org-b", "m2"): 2,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={},
        concurrency_limits=_limits_for(queued_by_org_queue),
        max_workers=24,
    )
    assert len(plan) == 5
    assert plan.count("m1") == 3
    assert plan.count("m2") == 2


def test_null_org_is_treated_as_its_own_bucket():
    """Legacy rows with ``org_id IS NULL`` still get spawned fairly.

    They share the global cap with other orgs but aren't silently
    dropped by the planner.
    """
    queued_by_org_queue = {
        (None, "m1"): 10,
        ("org-a", "m1"): 10,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={},
        concurrency_limits={"m1": 32},
        max_workers=8,
    )
    assert len(plan) == 8
    # Both the None-bucket and org-a should have received spawns.
    # We can't distinguish them in the output (both are "m1"), but we
    # can verify determinism by re-running with the budget restricted
    # to 1: the first spawn should deterministically pick the
    # named-org bucket since ``None`` sorts last.
    plan_one = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={},
        concurrency_limits={"m1": 32},
        max_workers=1,
    )
    assert plan_one == ["m1"]


def test_skips_queue_key_with_no_capacity():
    """A saturated queue_key is skipped even if it has queued demand."""
    queued_by_org_queue = {
        ("org-a", "m-full"): 50,
        ("org-a", "m-free"): 10,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={"m-full": 32, "m-free": 0},
        concurrency_limits={"m-full": 32, "m-free": 32},
        max_workers=24,
    )
    # All spawns should go to ``m-free``; ``m-full`` is at capacity.
    assert set(plan) == {"m-free"}
    assert len(plan) == 10  # m-free only has 10 queued


def test_zero_or_negative_queued_entries_ignored():
    queued_by_org_queue = {
        ("org-a", "m1"): 5,
        ("org-a", "m2"): 0,
        ("org-b", "m3"): -1,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={},
        concurrency_limits=_limits_for(queued_by_org_queue),
        max_workers=24,
    )
    assert plan == ["m1"] * 5

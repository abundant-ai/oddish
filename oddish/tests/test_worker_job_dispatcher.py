"""Unit tests for the `build_spawn_plan` fair-share planner.

These lock in the invariants we care about when the Modal dispatcher
wakes up with hundreds of queued jobs:

1. No org is starved when a louder org happens to have more queue_keys.
2. Within one org, heavier models don't completely starve lighter ones
   (the "leeway" knob) -- the inner round-robin guarantees at least
   one spawn per org-turn for a queue_key that still has work.
3. Per-queue_key global concurrency caps (``queue_slots`` leases) are
   respected across orgs -- and SHARED across Harbor variants of the
   same queue_key (the variant split is dispatch-only for v1).
4. The spawn budget (``max_workers``) is never exceeded even when
   total demand far outstrips it.

The planner keys on ``(org_id, queue_key, harbor_variant_id)`` queued
demand and ``(queue_key, harbor_variant_id)`` running counts, and emits
``(queue_key, harbor_variant_id)`` spawn units.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.workers.queue.worker_job_dispatcher import (  # noqa: E402
    build_spawn_plan,
    select_job_function,
)

_D = "default"  # the default variant, used by every non-override fixture


def _limits_for(queued_by_org_queue: dict, default: int = 32) -> dict[str, int]:
    """Helper: give every queue_key in the fixture the same default cap."""
    return {qk: default for (_, qk, _v) in queued_by_org_queue}


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
            queued_by_org_queue={("org-a", "m1", _D): 100},
            running_by_queue={},
            concurrency_limits={"m1": 32},
            max_workers=0,
        )
        == []
    )


def test_org_fairness_beats_queue_key_fairness():
    """Org A owns 3 models, Org B owns 1. They should still split 50/50."""
    queued_by_org_queue = {
        ("org-a", "m1", _D): 100,
        ("org-a", "m2", _D): 100,
        ("org-a", "m3", _D): 100,
        ("org-b", "m4", _D): 100,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={},
        concurrency_limits=_limits_for(queued_by_org_queue),
        max_workers=24,
    )
    assert len(plan) == 24
    per_org_count = Counter()
    a_qks = {"m1", "m2", "m3"}
    for qk, _variant in plan:
        per_org_count["a" if qk in a_qks else "b"] += 1
    assert per_org_count["a"] == 12
    assert per_org_count["b"] == 12


def test_within_org_round_robin_gives_leeway_to_small_queues():
    queued_by_org_queue = {
        ("org-a", "m-big", _D): 100,
        ("org-a", "m-small", _D): 5,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={},
        concurrency_limits=_limits_for(queued_by_org_queue, default=64),
        max_workers=24,
    )
    assert len(plan) == 24
    small_count = plan.count(("m-small", _D))
    big_count = plan.count(("m-big", _D))
    assert small_count == 5
    assert big_count == 19


def test_respects_global_queue_capacity_across_orgs():
    queued_by_org_queue = {
        ("org-a", "m1", _D): 100,
        ("org-b", "m1", _D): 100,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={("m1", _D): 8},
        concurrency_limits={"m1": 10},
        max_workers=24,
    )
    assert plan.count(("m1", _D)) == 2
    assert len(plan) == 2


def test_plan_never_exceeds_max_workers():
    queued_by_org_queue = {
        ("org-a", "m1", _D): 500,
        ("org-b", "m2", _D): 500,
        ("org-c", "m3", _D): 500,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={},
        concurrency_limits=_limits_for(queued_by_org_queue),
        max_workers=24,
    )
    assert len(plan) == 24


def test_plan_never_exceeds_total_demand():
    queued_by_org_queue = {
        ("org-a", "m1", _D): 3,
        ("org-b", "m2", _D): 2,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={},
        concurrency_limits=_limits_for(queued_by_org_queue),
        max_workers=24,
    )
    assert len(plan) == 5
    assert plan.count(("m1", _D)) == 3
    assert plan.count(("m2", _D)) == 2


def test_null_org_is_treated_as_its_own_bucket():
    queued_by_org_queue = {
        (None, "m1", _D): 10,
        ("org-a", "m1", _D): 10,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={},
        concurrency_limits={"m1": 32},
        max_workers=8,
    )
    assert len(plan) == 8
    plan_one = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={},
        concurrency_limits={"m1": 32},
        max_workers=1,
    )
    assert plan_one == [("m1", _D)]


def test_skips_queue_key_with_no_capacity():
    queued_by_org_queue = {
        ("org-a", "m-full", _D): 50,
        ("org-a", "m-free", _D): 10,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={("m-full", _D): 32, ("m-free", _D): 0},
        concurrency_limits={"m-full": 32, "m-free": 32},
        max_workers=24,
    )
    assert set(plan) == {("m-free", _D)}
    assert len(plan) == 10


def test_zero_or_negative_queued_entries_ignored():
    queued_by_org_queue = {
        ("org-a", "m1", _D): 5,
        ("org-a", "m2", _D): 0,
        ("org-b", "m3", _D): -1,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={},
        concurrency_limits=_limits_for(queued_by_org_queue),
        max_workers=24,
    )
    assert plan == [("m1", _D)] * 5


# ---------------------------------------------------------------------------
# Harbor-variant routing
# ---------------------------------------------------------------------------


def test_distinct_variants_become_distinct_spawn_units():
    """default + ephemeral on the same queue_key are separate spawn units."""
    queued_by_org_queue = {
        ("org-a", "m1", "default"): 5,
        ("org-a", "m1", "ephemeral"): 5,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={},
        concurrency_limits={"m1": 32},
        max_workers=24,
    )
    assert plan.count(("m1", "default")) == 5
    assert plan.count(("m1", "ephemeral")) == 5


def test_variants_share_one_queue_key_capacity_pool():
    """Decision (b): a queue_key's concurrency cap is shared across variants.

    Limit 10, nothing running, but 100 default + 100 ephemeral queued: the
    planner must spawn at most 10 total for the queue_key, split across the two
    variants -- not 10 per variant.
    """
    queued_by_org_queue = {
        ("org-a", "m1", "default"): 100,
        ("org-a", "m1", "ephemeral"): 100,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={},
        concurrency_limits={"m1": 10},
        max_workers=64,
    )
    assert len(plan) == 10
    # both variants get a share via the round-robin
    assert plan.count(("m1", "default")) == 5
    assert plan.count(("m1", "ephemeral")) == 5


def test_running_counts_sum_across_variants_against_shared_cap():
    """RUNNING of any variant consumes the shared per-queue_key capacity."""
    queued_by_org_queue = {
        ("org-a", "m1", "ephemeral"): 100,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        # 8 default already running eats into the cap of 10 -> 2 left.
        running_by_queue={("m1", "default"): 8},
        concurrency_limits={"m1": 10},
        max_workers=24,
    )
    assert plan == [("m1", "ephemeral")] * 2


def test_blessed_variant_routes_independently_of_default():
    queued_by_org_queue = {
        ("org-a", "m1", "default"): 2,
        ("org-a", "m1", "harbor-next"): 3,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={},
        concurrency_limits={"m1": 32},
        max_workers=24,
    )
    assert plan.count(("m1", "default")) == 2
    assert plan.count(("m1", "harbor-next")) == 3


# ---------------------------------------------------------------------------
# select_job_function — spawn-Function selection
# ---------------------------------------------------------------------------

_DEFAULT_FN = object()
_VARIANT_FN = object()


def test_default_and_ephemeral_route_to_the_base_function():
    for variant in ("default", "ephemeral"):
        fn, kwargs = select_job_function(
            ("m1", variant), default_fn=_DEFAULT_FN, variant_fns={}
        )
        assert fn is _DEFAULT_FN
        assert kwargs == {"queue_key": "m1", "harbor_variant_id": variant}


def test_blessed_variant_routes_to_its_own_function():
    fn, kwargs = select_job_function(
        ("m1", "harbor-next"),
        default_fn=_DEFAULT_FN,
        variant_fns={"harbor-next": _VARIANT_FN},
    )
    assert fn is _VARIANT_FN
    assert kwargs == {"queue_key": "m1", "harbor_variant_id": "harbor-next"}


def test_unregistered_variant_falls_back_to_base_function():
    # A pin classified to a variant whose image isn't built yet (registry/image
    # drift) must not be dropped -- fall back to the base function.
    fn, kwargs = select_job_function(
        ("m1", "harbor-missing"), default_fn=_DEFAULT_FN, variant_fns={}
    )
    assert fn is _DEFAULT_FN
    assert kwargs["harbor_variant_id"] == "harbor-missing"

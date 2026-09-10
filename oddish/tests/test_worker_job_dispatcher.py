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

Every planner key includes ``execution_lane`` so tests exercise the same exact
dispatch tuple used in production.
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.db import ACTIVE_WORKER_JOB_KINDS  # noqa: E402
from oddish.workers.queue import worker_job_dispatcher as dispatcher_module  # noqa: E402
from oddish.workers.queue.worker_job_dispatcher import (  # noqa: E402
    build_spawn_plan,
    discover_active_worker_job_queue_keys,
    get_worker_job_org_queue_counts,
    select_job_function,
)

_D = "default"  # the default variant, used by every non-override fixture


def test_dispatch_queries_only_count_active_worker_job_kinds(monkeypatch):
    class _Pool:
        def __init__(self):
            self.calls = []

        async def fetch(self, sql, *args):
            self.calls.append((" ".join(sql.split()), args))
            return []

    pool = _Pool()

    async def _get_pool():
        return pool

    monkeypatch.setattr(dispatcher_module, "get_pool", _get_pool)

    asyncio.run(discover_active_worker_job_queue_keys())
    asyncio.run(get_worker_job_org_queue_counts(("anthropic/claude-sonnet-5",)))

    active_kinds = tuple(kind.value for kind in ACTIVE_WORKER_JOB_KINDS)
    assert "QA" not in active_kinds
    assert len(pool.calls) == 3
    assert all("kind::text = ANY" in sql for sql, _args in pool.calls)
    assert all(args[-1] == active_kinds for _sql, args in pool.calls)


def _limits_for(queued_by_org_queue: dict, default: int = 32) -> dict[str, int]:
    """Helper: give every queue_key in the fixture the same default cap."""
    return {qk: default for (_, qk, _variant, _lane, _priority) in queued_by_org_queue}


def test_returns_empty_when_no_queued_work():
    assert [
        unit[:3]
        for unit in build_spawn_plan(
            queued_by_org_queue={},
            running_by_queue={},
            concurrency_limits={},
            max_workers=24,
        )
    ] == []


def test_returns_empty_when_budget_is_zero():
    assert [
        unit[:3]
        for unit in build_spawn_plan(
            queued_by_org_queue={("org-a", "m1", _D, "default", False): 100},
            running_by_queue={},
            concurrency_limits={"m1": 32},
            max_workers=0,
        )
    ] == []


def test_org_fairness_beats_queue_key_fairness():
    """Org A owns 3 models, Org B owns 1. They should still split 50/50."""
    queued_by_org_queue = {
        ("org-a", "m1", _D, "default", False): 100,
        ("org-a", "m2", _D, "default", False): 100,
        ("org-a", "m3", _D, "default", False): 100,
        ("org-b", "m4", _D, "default", False): 100,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={},
        concurrency_limits=_limits_for(queued_by_org_queue),
        max_workers=24,
    )
    plan = [unit[:3] for unit in plan]
    assert len(plan) == 24
    per_org_count = Counter()
    a_qks = {"m1", "m2", "m3"}
    for qk, _variant, _lane in plan:
        per_org_count["a" if qk in a_qks else "b"] += 1
    assert per_org_count["a"] == 12
    assert per_org_count["b"] == 12


def test_within_org_round_robin_gives_leeway_to_small_queues():
    queued_by_org_queue = {
        ("org-a", "m-big", _D, "default", False): 100,
        ("org-a", "m-small", _D, "default", False): 5,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={},
        concurrency_limits=_limits_for(queued_by_org_queue, default=64),
        max_workers=24,
    )
    plan = [unit[:3] for unit in plan]
    assert len(plan) == 24
    small_count = plan.count(("m-small", _D, "default"))
    big_count = plan.count(("m-big", _D, "default"))
    assert small_count == 5
    assert big_count == 19


def test_respects_global_queue_capacity_across_orgs():
    queued_by_org_queue = {
        ("org-a", "m1", _D, "default", False): 100,
        ("org-b", "m1", _D, "default", False): 100,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={("m1", _D, "default"): 8},
        concurrency_limits={"m1": 10},
        max_workers=24,
    )
    plan = [unit[:3] for unit in plan]
    assert plan.count(("m1", _D, "default")) == 2
    assert len(plan) == 2


def test_held_slots_count_against_queue_capacity():
    # Nothing RUNNING yet, but 8 queue_slots leases are held (freshly-spawned
    # workers that have not registered as RUNNING). Held is the authoritative
    # in-flight count, so only 10 - 8 = 2 more may spawn -- without it the planner
    # would over-spawn against a stale RUNNING=0 view.
    plan = build_spawn_plan(
        queued_by_org_queue={("org-a", "m1", _D, "default", False): 100},
        running_by_queue={},
        concurrency_limits={"m1": 10},
        max_workers=24,
        held_by_queue_key={"m1": 8},
    )
    plan = [unit[:3] for unit in plan]
    assert plan.count(("m1", _D, "default")) == 2
    assert len(plan) == 2


def test_capacity_uses_max_of_running_and_held_not_their_sum():
    # In-flight is max(RUNNING, held), never RUNNING + held: the lease and the
    # RUNNING row are two views of the SAME worker, so 3 running + 8 held is 8
    # in-flight (capacity 2), not 11 (which would wrongly spawn 0).
    plan = build_spawn_plan(
        queued_by_org_queue={("org-a", "m1", _D, "default", False): 100},
        running_by_queue={("m1", _D, "default"): 3},
        concurrency_limits={"m1": 10},
        max_workers=24,
        held_by_queue_key={"m1": 8},
    )
    plan = [unit[:3] for unit in plan]
    assert plan.count(("m1", _D, "default")) == 2

    # And when RUNNING already exceeds held, RUNNING dominates (held is stale).
    plan_running_dominates = build_spawn_plan(
        queued_by_org_queue={("org-a", "m1", _D, "default", False): 100},
        running_by_queue={("m1", _D, "default"): 8},
        concurrency_limits={"m1": 10},
        max_workers=24,
        held_by_queue_key={"m1": 3},
    )
    plan_running_dominates = [unit[:3] for unit in plan_running_dominates]
    assert plan_running_dominates.count(("m1", _D, "default")) == 2


def test_held_by_queue_key_defaults_to_running_only():
    # Omitting held_by_queue_key (the Modal poll_queue call) is unchanged: a
    # RUNNING-only capacity of 10 - 2 = 8.
    plan = build_spawn_plan(
        queued_by_org_queue={("org-a", "m1", _D, "default", False): 100},
        running_by_queue={("m1", _D, "default"): 2},
        concurrency_limits={"m1": 10},
        max_workers=24,
    )
    plan = [unit[:3] for unit in plan]
    assert plan.count(("m1", _D, "default")) == 8


def test_plan_never_exceeds_max_workers():
    queued_by_org_queue = {
        ("org-a", "m1", _D, "default", False): 500,
        ("org-b", "m2", _D, "default", False): 500,
        ("org-c", "m3", _D, "default", False): 500,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={},
        concurrency_limits=_limits_for(queued_by_org_queue),
        max_workers=24,
    )
    plan = [unit[:3] for unit in plan]
    assert len(plan) == 24


def test_lane_capacity_preserves_spawn_budget_for_other_lanes():
    queued_by_org_queue = {
        ("org-a", "ec2-model", _D, "ec2_trial", False): 100,
        ("org-a", "default-model", _D, "default", False): 100,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={},
        concurrency_limits=_limits_for(queued_by_org_queue),
        max_workers=4,
        capacity_limits_by_lane={"ec2_trial": 2},
        held_by_lane={"ec2_trial": 2},
    )
    plan = [unit[:3] for unit in plan]

    assert plan == [("default-model", _D, "default")] * 4


def test_lane_capacity_is_shared_across_queues_and_orgs():
    queued_by_org_queue = {
        ("org-a", "m1", _D, "ec2_trial", False): 100,
        ("org-b", "m2", _D, "ec2_trial", False): 100,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={("m1", _D, "ec2_trial"): 1},
        concurrency_limits=_limits_for(queued_by_org_queue),
        max_workers=24,
        capacity_limits_by_lane={"ec2_trial": 3},
        held_by_lane={"ec2_trial": 2},
    )
    plan = [unit[:3] for unit in plan]

    assert len(plan) == 1
    assert plan[0][2] == "ec2_trial"


def test_plan_never_exceeds_total_demand():
    queued_by_org_queue = {
        ("org-a", "m1", _D, "default", False): 3,
        ("org-b", "m2", _D, "default", False): 2,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={},
        concurrency_limits=_limits_for(queued_by_org_queue),
        max_workers=24,
    )
    plan = [unit[:3] for unit in plan]
    assert len(plan) == 5
    assert plan.count(("m1", _D, "default")) == 3
    assert plan.count(("m2", _D, "default")) == 2


def test_null_org_is_treated_as_its_own_bucket():
    queued_by_org_queue = {
        (None, "m1", _D, "default", False): 10,
        ("org-a", "m1", _D, "default", False): 10,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={},
        concurrency_limits={"m1": 32},
        max_workers=8,
    )
    plan = [unit[:3] for unit in plan]
    assert len(plan) == 8
    plan_one = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={},
        concurrency_limits={"m1": 32},
        max_workers=1,
    )
    plan_one = [unit[:3] for unit in plan_one]
    assert plan_one == [("m1", _D, "default")]


def test_skips_queue_key_with_no_capacity():
    queued_by_org_queue = {
        ("org-a", "m-full", _D, "default", False): 50,
        ("org-a", "m-free", _D, "default", False): 10,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={
            ("m-full", _D, "default"): 32,
            ("m-free", _D, "default"): 0,
        },
        concurrency_limits={"m-full": 32, "m-free": 32},
        max_workers=24,
    )
    plan = [unit[:3] for unit in plan]
    assert set(plan) == {("m-free", _D, "default")}
    assert len(plan) == 10


def test_zero_or_negative_queued_entries_ignored():
    queued_by_org_queue = {
        ("org-a", "m1", _D, "default", False): 5,
        ("org-a", "m2", _D, "default", False): 0,
        ("org-b", "m3", _D, "default", False): -1,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={},
        concurrency_limits=_limits_for(queued_by_org_queue),
        max_workers=24,
    )
    plan = [unit[:3] for unit in plan]
    assert plan == [("m1", _D, "default")] * 5


# ---------------------------------------------------------------------------
# Harbor-variant routing
# ---------------------------------------------------------------------------


def test_distinct_variants_become_distinct_spawn_units():
    """default + ephemeral on the same queue_key are separate spawn units."""
    queued_by_org_queue = {
        ("org-a", "m1", "default", "default", False): 5,
        ("org-a", "m1", "ephemeral", "default", False): 5,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={},
        concurrency_limits={"m1": 32},
        max_workers=24,
    )
    plan = [unit[:3] for unit in plan]
    assert plan.count(("m1", "default", "default")) == 5
    assert plan.count(("m1", "ephemeral", "default")) == 5


def test_variants_share_one_queue_key_capacity_pool():
    """Decision (b): a queue_key's concurrency cap is shared across variants.

    Limit 10, nothing running, but 100 default + 100 ephemeral queued: the
    planner must spawn at most 10 total for the queue_key, split across the two
    variants -- not 10 per variant.
    """
    queued_by_org_queue = {
        ("org-a", "m1", "default", "default", False): 100,
        ("org-a", "m1", "ephemeral", "default", False): 100,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={},
        concurrency_limits={"m1": 10},
        max_workers=64,
    )
    plan = [unit[:3] for unit in plan]
    assert len(plan) == 10
    # both variants get a share via the round-robin
    assert plan.count(("m1", "default", "default")) == 5
    assert plan.count(("m1", "ephemeral", "default")) == 5


def test_running_counts_sum_across_variants_against_shared_cap():
    """RUNNING of any variant consumes the shared per-queue_key capacity."""
    queued_by_org_queue = {
        ("org-a", "m1", "ephemeral", "default", False): 100,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        # 8 default already running eats into the cap of 10 -> 2 left.
        running_by_queue={("m1", "default", "default"): 8},
        concurrency_limits={"m1": 10},
        max_workers=24,
    )
    plan = [unit[:3] for unit in plan]
    assert plan == [("m1", "ephemeral", "default")] * 2


def test_blessed_variant_routes_independently_of_default():
    queued_by_org_queue = {
        ("org-a", "m1", "default", "default", False): 2,
        ("org-a", "m1", "harbor-next", "default", False): 3,
    }
    plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue={},
        concurrency_limits={"m1": 32},
        max_workers=24,
    )
    plan = [unit[:3] for unit in plan]
    assert plan.count(("m1", "default", "default")) == 2
    assert plan.count(("m1", "harbor-next", "default")) == 3


# ---------------------------------------------------------------------------
# select_job_function — spawn-Function selection
# ---------------------------------------------------------------------------

_DEFAULT_FN = object()
_VARIANT_FN = object()
_EC2_FN = object()
_EC2_VARIANT_FN = object()


def test_default_and_ephemeral_route_to_the_base_function():
    for variant in ("default", "ephemeral"):
        fn, kwargs = select_job_function(
            dispatcher_module.DispatchUnit(*("m1", variant, "default"), False, None),
            default_fn=_DEFAULT_FN,
            variant_fns={},
        )
        assert fn is _DEFAULT_FN
        assert kwargs == {
            "queue_key": "m1",
            "harbor_variant_id": variant,
            "execution_lane": "default",
            "priority_class": False,
            "org_id": None,
        }


def test_blessed_variant_routes_to_its_own_function():
    fn, kwargs = select_job_function(
        dispatcher_module.DispatchUnit(*("m1", "harbor-next", "default"), False, None),
        default_fn=_DEFAULT_FN,
        variant_fns={"harbor-next": _VARIANT_FN},
    )
    assert fn is _VARIANT_FN
    assert kwargs == {
        "queue_key": "m1",
        "harbor_variant_id": "harbor-next",
        "execution_lane": "default",
        "priority_class": False,
        "org_id": None,
    }


def test_unregistered_variant_falls_back_to_base_function():
    # A pin classified to a variant whose image isn't built yet (registry/image
    # drift) must not be dropped -- fall back to the base function.
    fn, kwargs = select_job_function(
        dispatcher_module.DispatchUnit(
            *("m1", "harbor-missing", "default"), False, None
        ),
        default_fn=_DEFAULT_FN,
        variant_fns={},
    )
    assert fn is _DEFAULT_FN
    assert kwargs["harbor_variant_id"] == "harbor-missing"


def test_ec2_lane_routes_only_to_ec2_credential_function():
    fn, kwargs = select_job_function(
        dispatcher_module.DispatchUnit(*("m1", "default", "ec2_trial"), False, None),
        default_fn=_DEFAULT_FN,
        ec2_fn=_EC2_FN,
        variant_fns={},
        ec2_variant_fns={},
    )
    assert fn is _EC2_FN
    assert kwargs == {
        "queue_key": "m1",
        "harbor_variant_id": "default",
        "execution_lane": "ec2_trial",
        "priority_class": False,
        "org_id": None,
    }


def test_ec2_blessed_variant_stays_in_ec2_credential_topology():
    fn, kwargs = select_job_function(
        dispatcher_module.DispatchUnit(
            *("m1", "harbor-next", "ec2_trial"), False, None
        ),
        default_fn=_DEFAULT_FN,
        ec2_fn=_EC2_FN,
        variant_fns={"harbor-next": _VARIANT_FN},
        ec2_variant_fns={"harbor-next": _EC2_VARIANT_FN},
    )
    assert fn is _EC2_VARIANT_FN
    assert kwargs["execution_lane"] == "ec2_trial"


def test_full_lane_keys_are_preserved_by_spawn_plan():
    plan = build_spawn_plan(
        queued_by_org_queue={
            ("org-a", "m1", "default", "default", False): 1,
            ("org-a", "m1", "default", "ec2_trial", False): 1,
        },
        running_by_queue={},
        concurrency_limits={"m1": 2},
        max_workers=2,
    )
    plan = [unit[:3] for unit in plan]
    assert set(plan) == {
        ("m1", "default", "default"),
        ("m1", "default", "ec2_trial"),
    }


def test_analysis_gets_three_of_four_turns_under_one_shared_model_cap():
    plan = dispatcher_module.build_spawn_plan(
        {
            ("org", "sonnet", _D, "default", True): 300,
            ("org", "sonnet", _D, "default", False): 300,
        },
        {},
        {"sonnet": 600},
        256,
    )
    assert Counter(unit.priority_class for unit in plan) == {True: 192, False: 64}
    assert {unit.queue_key for unit in plan} == {"sonnet"}


def test_continuous_analysis_arrivals_cannot_reset_ordinary_turn():
    cursors = {}
    classes = []
    for _ in range(40):
        # Demand is replenished each cycle; only one free launch is available.
        [unit] = dispatcher_module.build_spawn_plan(
            {
                ("org", "qa", _D, "default", True): 300,
                ("org", "solver", _D, "default", False): 300,
            },
            {},
            {"qa": 600, "solver": 600},
            1,
            fairness_cursors=cursors,
        )
        classes.append(unit.priority_class)
    assert classes == [True, True, True, False] * 10


def test_priority_preference_preserves_org_fairness_and_lends_empty_turns():
    plan = dispatcher_module.build_spawn_plan(
        {
            ("org-a", "qa", _D, "default", True): 300,
            ("org-a", "solver", _D, "default", False): 300,
            ("org-b", "other", _D, "default", False): 300,
        },
        {},
        {"qa": 600, "solver": 600, "other": 600},
        256,
    )
    assert Counter(unit.org_id for unit in plan) == {"org-a": 128, "org-b": 128}
    assert Counter(unit.priority_class for unit in plan if unit.org_id == "org-a") == {
        True: 96,
        False: 32,
    }

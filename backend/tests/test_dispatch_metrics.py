from __future__ import annotations

import asyncio
import types
from contextlib import nullcontext
from typing import Any

import pytest

from oddish.dispatch.cycle import DispatchPlan
from oddish.workers.queue.slots import LaunchReservation
from oddish.workers.queue.worker_job_dispatcher import DispatchUnit
from worker import functions as worker_functions


@pytest.mark.asyncio
async def test_modal_poll_records_success_and_post_spawn_failure(monkeypatch) -> None:
    plan = DispatchPlan(
        queue_units=(("gpt-5", "default", "default"),),
        queue_keys=("gpt-5",),
        queued_by_org_queue={(None, "gpt-5", "default", "default", False): 2},
        running_by_queue={("gpt-5", "default", "default"): 0},
        queued_by_queue={"gpt-5": 2},
        running_by_queue_key={"gpt-5": 0},
        held_by_queue_key={"gpt-5": 0},
        concurrency_limits={"gpt-5": 2},
        unit_plan=[DispatchUnit("gpt-5", "default", "default", False, None)],
    )
    snapshots: list[dict[str, Any]] = []
    cycles: list[dict[str, Any]] = []
    spawn_kwargs: list[dict[str, Any]] = []

    async def no_op(*_args, **_kwargs):
        return None

    async def reserve_plan(_build_plan):
        return plan, [LaunchReservation(plan.unit_plan[0], 0, "launch-token")]

    async def spawn_aio(**kwargs):
        spawn_kwargs.append(kwargs)
        return object()

    spawn_function = types.SimpleNamespace(spawn=types.SimpleNamespace(aio=spawn_aio))

    monkeypatch.setattr(worker_functions, "_otel_span", lambda *_a, **_k: nullcontext())
    monkeypatch.setattr(worker_functions, "configure_storage_paths", no_op)
    monkeypatch.setattr(worker_functions, "reserve_queue_launches", reserve_plan)
    monkeypatch.setattr(worker_functions, "record_queue_runtime_status", no_op)
    monkeypatch.setattr(worker_functions, "stamp_dispatch_stage", no_op)
    monkeypatch.setattr(worker_functions, "close_database_connections", no_op)
    monkeypatch.setattr(worker_functions, "release_launch_reservations", no_op)
    monkeypatch.setattr(worker_functions.settings, "ec2_enabled", False)
    monkeypatch.setattr(worker_functions, "MAX_WORKERS_PER_POLL", 1)
    monkeypatch.setattr(
        worker_functions,
        "select_job_function",
        lambda _unit, **_functions: (spawn_function, {"queue_key": "gpt-5"}),
    )
    monkeypatch.setattr(
        worker_functions,
        "record_dispatch_snapshot",
        lambda **values: snapshots.append(values),
    )
    monkeypatch.setattr(
        worker_functions,
        "record_dispatch_cycle",
        lambda **values: cycles.append(values),
    )

    await worker_functions.poll_queue.get_raw_f()()

    assert spawn_kwargs == [{"queue_key": "gpt-5", "reservation_token": "launch-token"}]
    assert snapshots == [
        {
            "queue_keys": plan.queue_keys,
            "queued_by_queue": plan.queued_by_queue,
            "running_by_queue_key": plan.running_by_queue_key,
            "held_by_queue_key": plan.held_by_queue_key,
            "concurrency_limits": plan.concurrency_limits,
        }
    ]
    assert len(cycles) == 1
    assert cycles[0]["workers_spawned"] == 1
    assert cycles[0]["spawn_cap_reached"] is True
    assert cycles[0]["outcome"] == "success"
    assert cycles[0]["duration_seconds"] >= 0

    why_waiting_calls = 0

    def fail_after_spawn(*_args, **_kwargs):
        nonlocal why_waiting_calls
        why_waiting_calls += 1
        if why_waiting_calls == 2:
            raise RuntimeError("post-spawn failed")
        return {}

    monkeypatch.setattr(
        worker_functions,
        "compute_post_spawn_why_waiting",
        fail_after_spawn,
    )

    with pytest.raises(RuntimeError, match="post-spawn failed"):
        await worker_functions.poll_queue.get_raw_f()()

    assert len(cycles) == 2
    assert cycles[1]["workers_spawned"] == 1
    assert cycles[1]["spawn_cap_reached"] is True
    assert cycles[1]["outcome"] == "error"


@pytest.mark.asyncio
async def test_modal_poll_records_transient_oserror_as_skipped(monkeypatch) -> None:
    cycles: list[dict[str, Any]] = []

    async def no_op(*_args, **_kwargs):
        return None

    async def fail_plan(_build_plan):
        raise OSError("temporary DNS failure")

    monkeypatch.setattr(worker_functions, "_otel_span", lambda *_a, **_k: nullcontext())
    monkeypatch.setattr(worker_functions, "configure_storage_paths", no_op)
    monkeypatch.setattr(worker_functions, "reserve_queue_launches", fail_plan)
    monkeypatch.setattr(worker_functions, "close_database_connections", no_op)
    monkeypatch.setattr(worker_functions, "release_launch_reservations", no_op)
    monkeypatch.setattr(worker_functions.settings, "ec2_enabled", False)
    monkeypatch.setattr(
        worker_functions,
        "record_dispatch_cycle",
        lambda **values: cycles.append(values),
    )

    await worker_functions.poll_queue.get_raw_f()()

    assert len(cycles) == 1
    assert cycles[0]["workers_spawned"] == 0
    assert cycles[0]["spawn_cap_reached"] is False
    assert cycles[0]["outcome"] == "skipped"


@pytest.mark.asyncio
async def test_modal_poll_records_cancellation_and_propagates(monkeypatch) -> None:
    cycles: list[dict[str, Any]] = []

    async def no_op(*_args, **_kwargs):
        return None

    async def cancel_plan(_build_plan):
        raise asyncio.CancelledError

    monkeypatch.setattr(worker_functions, "_otel_span", lambda *_a, **_k: nullcontext())
    monkeypatch.setattr(worker_functions, "configure_storage_paths", no_op)
    monkeypatch.setattr(worker_functions, "reserve_queue_launches", cancel_plan)
    monkeypatch.setattr(worker_functions, "close_database_connections", no_op)
    monkeypatch.setattr(worker_functions, "release_launch_reservations", no_op)
    monkeypatch.setattr(worker_functions.settings, "ec2_enabled", False)
    monkeypatch.setattr(
        worker_functions,
        "record_dispatch_cycle",
        lambda **values: cycles.append(values),
    )

    with pytest.raises(asyncio.CancelledError):
        await worker_functions.poll_queue.get_raw_f()()

    assert len(cycles) == 1
    assert cycles[0]["workers_spawned"] == 0
    assert cycles[0]["spawn_cap_reached"] is False
    assert cycles[0]["outcome"] == "cancelled"
    assert cycles[0]["duration_seconds"] >= 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [RuntimeError("launch failed"), asyncio.CancelledError()]
)
async def test_partial_launch_failure_releases_only_failed_tokens(monkeypatch, error):
    units = [DispatchUnit("sonnet", "default", "default", True, "org")] * 2
    reservations = [
        LaunchReservation(units[0], 0, "ok"),
        LaunchReservation(units[1], 1, "failed"),
    ]
    plan = DispatchPlan(
        queue_units=(("sonnet", "default", "default"),),
        queue_keys=("sonnet",),
        queued_by_org_queue={("org", "sonnet", "default", "default", True): 2},
        running_by_queue={},
        queued_by_queue={"sonnet": 2},
        running_by_queue_key={},
        held_by_queue_key={},
        concurrency_limits={"sonnet": 2},
        unit_plan=units,
    )
    released, statuses, cycles = [], [], []

    async def no_op(*args, **kwargs):
        pass

    async def reserve(_build_plan):
        return plan, reservations

    async def release(tokens):
        released.extend(tokens)

    async def status(_component, payload):
        statuses.append(payload)

    async def spawn(**kwargs):
        if kwargs["reservation_token"] == "failed":
            raise error
        return object()

    fn = types.SimpleNamespace(spawn=types.SimpleNamespace(aio=spawn))
    monkeypatch.setattr(worker_functions, "_otel_span", lambda *_a, **_k: nullcontext())
    for name in (
        "configure_storage_paths",
        "stamp_dispatch_stage",
        "close_database_connections",
    ):
        monkeypatch.setattr(worker_functions, name, no_op)
    monkeypatch.setattr(worker_functions.settings, "ec2_enabled", False)
    monkeypatch.setattr(worker_functions, "reserve_queue_launches", reserve)
    monkeypatch.setattr(worker_functions, "release_launch_reservations", release)
    monkeypatch.setattr(worker_functions, "record_queue_runtime_status", status)
    monkeypatch.setattr(worker_functions, "record_dispatch_snapshot", lambda **_k: None)
    monkeypatch.setattr(
        worker_functions, "record_dispatch_cycle", lambda **kw: cycles.append(kw)
    )
    monkeypatch.setattr(
        worker_functions, "select_job_function", lambda _unit, **_k: (fn, {})
    )
    with pytest.raises(type(error)):
        await worker_functions.poll_queue.get_raw_f()()
    assert released == ["failed"]
    assert statuses[-1]["spawned"] == cycles[-1]["workers_spawned"] == 1
    assert statuses[-1]["launch_failed"] == 1

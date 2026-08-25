from __future__ import annotations

import types
from contextlib import nullcontext
from typing import Any

import pytest

from oddish.dispatch.cycle import DispatchPlan
from worker import functions as worker_functions


@pytest.mark.asyncio
async def test_modal_poll_records_success_and_post_spawn_failure(monkeypatch) -> None:
    plan = DispatchPlan(
        queue_units=(("gpt-5", "default", "default"),),
        queue_keys=("gpt-5",),
        queued_by_org_queue={(None, "gpt-5", "default", "default"): 2},
        running_by_queue={("gpt-5", "default", "default"): 0},
        queued_by_queue={"gpt-5": 2},
        running_by_queue_key={"gpt-5": 0},
        held_by_queue_key={"gpt-5": 0},
        concurrency_limits={"gpt-5": 2},
        unit_plan=[("gpt-5", "default", "default")],
    )
    snapshots: list[dict[str, Any]] = []
    cycles: list[dict[str, Any]] = []
    spawn_kwargs: list[dict[str, Any]] = []

    async def no_op(*_args, **_kwargs):
        return None

    async def build_plan(**_kwargs):
        return plan

    async def spawn_aio(**kwargs):
        spawn_kwargs.append(kwargs)
        return object()

    spawn_function = types.SimpleNamespace(spawn=types.SimpleNamespace(aio=spawn_aio))

    monkeypatch.setattr(worker_functions, "_otel_span", lambda *_a, **_k: nullcontext())
    monkeypatch.setattr(worker_functions, "configure_storage_paths", no_op)
    monkeypatch.setattr(worker_functions, "build_dispatch_plan", build_plan)
    monkeypatch.setattr(worker_functions, "record_queue_runtime_status", no_op)
    monkeypatch.setattr(worker_functions, "stamp_dispatch_stage", no_op)
    monkeypatch.setattr(worker_functions, "close_database_connections", no_op)
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

    assert spawn_kwargs == [{"queue_key": "gpt-5"}]
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

    async def fail_plan(**_kwargs):
        raise OSError("temporary DNS failure")

    monkeypatch.setattr(worker_functions, "_otel_span", lambda *_a, **_k: nullcontext())
    monkeypatch.setattr(worker_functions, "configure_storage_paths", no_op)
    monkeypatch.setattr(worker_functions, "build_dispatch_plan", fail_plan)
    monkeypatch.setattr(worker_functions, "close_database_connections", no_op)
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

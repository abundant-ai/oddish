from __future__ import annotations

import asyncio
from contextlib import nullcontext

import pytest
from oddish.runtime.sandbox_lifecycle import EC2_TRIAL_EXECUTION_LANE


def _patch_worker_dependencies(monkeypatch, worker_functions):
    released_queue_slots: list[tuple[str, int, str]] = []
    released_capacity_slots: list[tuple[str, int, str]] = []

    async def _noop(*args, **kwargs):
        return None

    async def _effective_model_concurrency(queue_key):
        return 1

    async def _acquire_slot(**kwargs):
        return 0

    async def _release_queue_slot(*, queue_key, slot, worker_id):
        released_queue_slots.append((queue_key, slot, worker_id))
        return True

    async def _release_capacity_slot(*, provider, slot, worker_id):
        released_capacity_slots.append((provider, slot, worker_id))
        return True

    monkeypatch.setattr(
        worker_functions.modal, "current_function_call_id", lambda: "fc-test"
    )
    monkeypatch.setattr(
        worker_functions, "_otel_span", lambda *args, **kwargs: nullcontext()
    )
    monkeypatch.setattr(worker_functions, "configure_storage_paths", _noop)
    monkeypatch.setattr(worker_functions, "close_database_connections", _noop)
    monkeypatch.setattr(
        worker_functions,
        "_effective_model_concurrency",
        _effective_model_concurrency,
    )
    monkeypatch.setattr(
        worker_functions,
        "acquire_sandbox_capacity_lease",
        _acquire_slot,
    )
    monkeypatch.setattr(
        worker_functions,
        "acquire_queue_slot",
        _acquire_slot,
    )
    monkeypatch.setattr(worker_functions, "release_queue_slot", _release_queue_slot)
    monkeypatch.setattr(
        worker_functions,
        "release_sandbox_capacity_lease",
        _release_capacity_slot,
    )
    monkeypatch.setattr(worker_functions.settings, "ec2_max_concurrent_instances", 1)
    return released_queue_slots, released_capacity_slots


def test_cancelled_ec2_worker_preserves_capacity_lease(monkeypatch) -> None:
    from worker import functions as worker_functions

    released_queue_slots, released_capacity_slots = _patch_worker_dependencies(
        monkeypatch, worker_functions
    )

    async def _cancelled_job(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(worker_functions, "run_single_worker_job", _cancelled_job)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            worker_functions._run_one_job(
                "fireworks/glm-5p2",
                execution_lane=EC2_TRIAL_EXECUTION_LANE,
            )
        )

    assert len(released_queue_slots) == 1
    assert released_capacity_slots == []


def test_completed_ec2_worker_releases_capacity_lease(monkeypatch) -> None:
    from worker import functions as worker_functions

    released_queue_slots, released_capacity_slots = _patch_worker_dependencies(
        monkeypatch, worker_functions
    )

    async def _completed_job(*args, **kwargs):
        return True

    monkeypatch.setattr(worker_functions, "run_single_worker_job", _completed_job)

    asyncio.run(
        worker_functions._run_one_job(
            "fireworks/glm-5p2",
            execution_lane=EC2_TRIAL_EXECUTION_LANE,
        )
    )

    assert len(released_queue_slots) == 1
    assert len(released_capacity_slots) == 1

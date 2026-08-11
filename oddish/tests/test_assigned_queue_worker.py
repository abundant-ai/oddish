from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.workers.queue import queue_manager
from oddish.workers.queue import worker as worker_module


def _limit(value):
    async def load(queue_key):
        return value

    return load


def test_assigned_queue_worker_acquires_drains_and_releases(monkeypatch) -> None:
    events: list[str] = []

    async def _acquire(*, queue_key, limit, worker_id, lease_seconds):
        events.append(f"acquire:{queue_key}:limit={limit}")
        return 3  # slot index

    async def _release(*, queue_key, slot, worker_id):
        events.append(f"release:{queue_key}:slot={slot}")

    async def _drain(queue_key, *, worker_id, queue_slot, budget_seconds, **kwargs):
        events.append(f"drain:{queue_key}:slot={queue_slot}")
        return 2

    monkeypatch.setattr(queue_manager, "acquire_queue_slot", _acquire)
    monkeypatch.setattr(queue_manager, "release_queue_slot", _release)
    monkeypatch.setattr(queue_manager, "drain_worker_jobs", _drain)
    monkeypatch.setattr(
        queue_manager, "load_effective_model_concurrency_limit", _limit(5)
    )

    processed = asyncio.run(queue_manager.run_assigned_queue_worker("gpt-4o"))

    assert processed == 2
    assert events == [
        "acquire:gpt-4o:limit=5",
        "drain:gpt-4o:slot=3",
        "release:gpt-4o:slot=3",
    ]


def test_assigned_queue_worker_zero_limit_skips(monkeypatch) -> None:
    monkeypatch.setattr(
        queue_manager, "load_effective_model_concurrency_limit", _limit(0)
    )

    async def _boom(**kwargs):  # pragma: no cover - must not be called
        raise AssertionError("should not acquire a slot when limit is 0")

    monkeypatch.setattr(queue_manager, "acquire_queue_slot", _boom)
    assert asyncio.run(queue_manager.run_assigned_queue_worker("x")) == 0


def test_assigned_queue_worker_releases_slot_even_on_drain_error(monkeypatch) -> None:
    released: list[int] = []

    async def _acquire(*, queue_key, limit, worker_id, lease_seconds):
        return 0

    async def _release(*, queue_key, slot, worker_id):
        released.append(slot)

    async def _drain(*args, **kwargs):
        raise RuntimeError("drain blew up")

    monkeypatch.setattr(queue_manager, "acquire_queue_slot", _acquire)
    monkeypatch.setattr(queue_manager, "release_queue_slot", _release)
    monkeypatch.setattr(queue_manager, "drain_worker_jobs", _drain)
    monkeypatch.setattr(
        queue_manager, "load_effective_model_concurrency_limit", _limit(1)
    )

    try:
        asyncio.run(queue_manager.run_assigned_queue_worker("x"))
    except RuntimeError:
        pass
    assert released == [0]  # slot released despite the drain error


def test_assigned_queue_worker_no_slot_available(monkeypatch) -> None:
    async def _acquire(*, queue_key, limit, worker_id, lease_seconds):
        return None  # contended -> no slot

    monkeypatch.setattr(queue_manager, "acquire_queue_slot", _acquire)
    monkeypatch.setattr(
        queue_manager, "load_effective_model_concurrency_limit", _limit(1)
    )
    assert asyncio.run(queue_manager.run_assigned_queue_worker("x")) == 0


def test_assigned_ec2_worker_claims_exact_lane_and_releases_capacity(
    monkeypatch,
) -> None:
    events: list[str] = []
    drained: dict = {}

    async def _acquire_capacity(**kwargs):
        events.append("capacity-acquire")
        return 4

    async def _release_capacity(**kwargs):
        events.append("capacity-release")

    async def _acquire_slot(**kwargs):
        events.append("queue-acquire")
        return 2

    async def _release_slot(**kwargs):
        events.append("queue-release")

    async def _drain(queue_key, **kwargs):
        drained.update(kwargs)
        events.append("drain")
        return 1

    monkeypatch.setattr(
        queue_manager, "acquire_sandbox_capacity_lease", _acquire_capacity
    )
    monkeypatch.setattr(
        queue_manager, "release_sandbox_capacity_lease", _release_capacity
    )
    monkeypatch.setattr(queue_manager, "acquire_queue_slot", _acquire_slot)
    monkeypatch.setattr(queue_manager, "release_queue_slot", _release_slot)
    monkeypatch.setattr(queue_manager, "drain_worker_jobs", _drain)
    monkeypatch.setattr(
        queue_manager, "load_effective_model_concurrency_limit", _limit(3)
    )

    processed = asyncio.run(
        queue_manager.run_assigned_queue_worker(
            "zai/glm-5.2",
            worker_id="worker-1",
            harbor_variant_id="harbor-next",
            execution_lane="ec2_trial",
        )
    )

    assert processed == 1
    assert drained["harbor_variant_id"] == "harbor-next"
    assert drained["execution_lane"] == "ec2_trial"
    assert drained["capacity_provider"] == "ec2"
    assert drained["capacity_slot"] == 4
    assert events == [
        "capacity-acquire",
        "queue-acquire",
        "drain",
        "queue-release",
        "capacity-release",
    ]


def test_worker_entrypoint_forwards_assigned_variant_and_lane(monkeypatch) -> None:
    received: dict = {}

    async def _run_assigned(queue_key, **kwargs):
        received["queue_key"] = queue_key
        received.update(kwargs)
        return 1

    async def _close_pool():
        return None

    monkeypatch.setenv(worker_module.ASSIGNED_QUEUE_KEY_ENV, "zai/glm-5.2")
    monkeypatch.setenv(worker_module.ASSIGNED_HARBOR_VARIANT_ENV, "harbor-next")
    monkeypatch.setenv(worker_module.ASSIGNED_EXECUTION_LANE_ENV, "ec2_trial")
    monkeypatch.setattr(worker_module, "run_assigned_queue_worker", _run_assigned)
    monkeypatch.setattr(worker_module, "close_pool", _close_pool)
    monkeypatch.setattr(worker_module, "configure_observability", lambda *_: None)
    monkeypatch.setattr(worker_module, "log_local_storage_snapshot", lambda *_: None)
    monkeypatch.setattr(
        worker_module.asyncio,
        "get_event_loop",
        lambda: SimpleNamespace(add_signal_handler=lambda *_: None),
    )

    asyncio.run(worker_module.run_worker())

    assert received == {
        "queue_key": "zai/glm-5.2",
        "harbor_variant_id": "harbor-next",
        "execution_lane": "ec2_trial",
    }

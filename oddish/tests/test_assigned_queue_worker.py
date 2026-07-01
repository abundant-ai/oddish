from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.workers.queue import queue_manager


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
        queue_manager, "settings", types.SimpleNamespace(get_model_concurrency=lambda qk: 5)
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
        queue_manager, "settings", types.SimpleNamespace(get_model_concurrency=lambda qk: 0)
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
        queue_manager, "settings", types.SimpleNamespace(get_model_concurrency=lambda qk: 1)
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
        queue_manager, "settings", types.SimpleNamespace(get_model_concurrency=lambda qk: 1)
    )
    assert asyncio.run(queue_manager.run_assigned_queue_worker("x")) == 0

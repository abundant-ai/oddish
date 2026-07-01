from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.dispatch.backends.fake import FakeDispatcher
from oddish.dispatch.cycle import run_dispatch_cycle
from oddish.workers.queue import worker_job_dispatcher as wjd


class _FakePool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args) -> None:
        self.calls.append((" ".join(sql.split()), args))


def test_stamp_dispatch_stage_marks_spawned_and_reasons(monkeypatch) -> None:
    pool = _FakePool()

    async def _get_pool():
        return pool

    monkeypatch.setattr(wjd, "get_pool", _get_pool)

    asyncio.run(
        wjd.stamp_dispatch_stage(
            ["gpt-4o", "gpt-4o"], {"busy": "waiting for slot (limit 2, running 2)"}
        )
    )

    spawn_calls = [c for c in pool.calls if "spawned_at = NOW()" in c[0]]
    reason_calls = [c for c in pool.calls if "admission_reason = $2" in c[0]]
    assert len(spawn_calls) == 1
    # one LIMITed stamp per queue_key, carrying its per-key spawn count (2)
    assert spawn_calls[0][1] == ("gpt-4o", 2)
    assert "spawned_at IS NULL" in spawn_calls[0][0]
    assert "LIMIT" in spawn_calls[0][0]
    assert len(reason_calls) == 1
    assert reason_calls[0][1] == ("busy", "waiting for slot (limit 2, running 2)")


def test_stamp_dispatch_stage_stamps_only_per_key_spawn_count(monkeypatch) -> None:
    # Partial serve: 2 workers for "a", 1 for "b" -> one LIMITed stamp per key
    # bounded by that key's spawn count, never every QUEUED/RETRYING row.
    pool = _FakePool()

    async def _get_pool():
        return pool

    monkeypatch.setattr(wjd, "get_pool", _get_pool)
    asyncio.run(wjd.stamp_dispatch_stage(["a", "b", "a"], {}))

    spawn_calls = [c for c in pool.calls if "spawned_at = NOW()" in c[0]]
    by_key = {c[1][0]: c[1][1] for c in spawn_calls}
    assert by_key == {"a": 2, "b": 1}
    assert all("LIMIT" in c[0] and "spawned_at IS NULL" in c[0] for c in spawn_calls)


def test_stamp_dispatch_stage_no_spawns_only_reasons(monkeypatch) -> None:
    pool = _FakePool()

    async def _get_pool():
        return pool

    monkeypatch.setattr(wjd, "get_pool", _get_pool)
    asyncio.run(wjd.stamp_dispatch_stage([], {"q": "cold-starting"}))

    assert not [c for c in pool.calls if "spawned_at = NOW()" in c[0]]
    assert [c for c in pool.calls if "admission_reason = $2" in c[0]]


def test_run_dispatch_cycle_invokes_on_stage_with_admitted_and_reasons() -> None:
    dispatcher = FakeDispatcher()
    seen: dict[str, object] = {}

    async def _on_stage(spawned_keys, why_waiting) -> None:
        seen["spawned"] = list(spawned_keys)
        seen["why_waiting"] = dict(why_waiting)

    async def _discover():
        return (("gpt-4o", "default"),)

    async def _counts(_keys):
        return {(None, "gpt-4o", "default"): 2}, {("gpt-4o", "default"): 0}

    async def _held(_keys):
        return {}

    asyncio.run(
        run_dispatch_cycle(
            dispatcher,
            max_workers=10,
            concurrency_for=lambda qk: 5,
            on_stage=_on_stage,
            _discover=_discover,
            _counts=_counts,
            _held=_held,
        )
    )
    assert seen["spawned"] == ["gpt-4o", "gpt-4o"]
    assert seen["why_waiting"] == {}


def test_run_dispatch_cycle_on_stage_stamps_spawned_handles_not_admitted() -> None:
    # A backend that returns FEWER handles than admitted (a partial spawn) must
    # stamp spawned_at only for the queue_keys that actually got a worker, not
    # the whole admitted plan -- otherwise rows whose worker never started get a
    # spurious spawned_at.
    class _PartialDispatcher(FakeDispatcher):
        async def spawn(self, *, spawn_plan):
            # Only the first admitted key actually gets a worker this cycle.
            return list(await super().spawn(spawn_plan=list(spawn_plan)[:1]))

    dispatcher = _PartialDispatcher()
    seen: dict[str, object] = {}

    async def _on_stage(spawned_keys, why_waiting) -> None:
        seen["spawned"] = list(spawned_keys)

    async def _discover():
        return (("gpt-4o", "default"),)

    async def _counts(_keys):
        return {(None, "gpt-4o", "default"): 3}, {("gpt-4o", "default"): 0}

    async def _held(_keys):
        return {}

    result = asyncio.run(
        run_dispatch_cycle(
            dispatcher,
            max_workers=10,
            concurrency_for=lambda qk: 5,
            on_stage=_on_stage,
            _discover=_discover,
            _counts=_counts,
            _held=_held,
        )
    )
    # admitted is 3 workers (3 queued, capacity 5), but only one handle came
    # back -> on_stage sees the single spawned queue_key, not all three.
    assert result.admitted == ["gpt-4o", "gpt-4o", "gpt-4o"]
    assert [h.queue_key for h in result.handles] == ["gpt-4o"]
    assert seen["spawned"] == ["gpt-4o"]

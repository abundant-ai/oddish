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
    # de-duplicated queue_keys passed as a single ANY($1) array
    assert sorted(spawn_calls[0][1][0]) == ["gpt-4o"]
    assert "spawned_at IS NULL" in spawn_calls[0][0]
    assert len(reason_calls) == 1
    assert reason_calls[0][1] == ("busy", "waiting for slot (limit 2, running 2)")


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

    asyncio.run(
        run_dispatch_cycle(
            dispatcher,
            max_workers=10,
            concurrency_for=lambda qk: 5,
            on_stage=_on_stage,
            _discover=_discover,
            _counts=_counts,
        )
    )
    assert seen["spawned"] == ["gpt-4o", "gpt-4o"]
    assert seen["why_waiting"] == {}

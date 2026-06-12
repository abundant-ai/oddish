"""Tests for public-endpoint tag visibility filtering."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _run(coro):
    return asyncio.run(coro)


class _FakeSession:
    def __init__(self):
        self.executed: list[tuple[str, dict]] = []

    async def execute(self, stmt, params=None):
        self.executed.append((str(stmt), dict(params or {})))

        class _R:
            def all(self_inner):
                return []

        return _R()


def test_public_task_endpoint_uses_public_only_resolver(monkeypatch):
    from oddish.core import public as public_module

    captured = {}

    async def _fake_list(session, *, task_ids, public_only):
        captured["public_only"] = public_only
        captured["task_ids"] = list(task_ids)
        return {tid: [] for tid in task_ids}

    monkeypatch.setattr(
        public_module,
        "list_effective_user_tags_for_task_versions",
        _fake_list,
    )
    session = _FakeSession()
    _run(
        public_module._hydrate_public_user_tags(  # type: ignore[attr-defined]
            session, task_ids=["task-1", "task-2"]
        )
    )
    assert captured["public_only"] is True
    assert captured["task_ids"] == ["task-1", "task-2"]

"""Tests for tag_exclusion creation/removal in tags_core."""

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

            @property
            def rowcount(self_inner):
                return 1

        return _R()

    async def scalar(self, stmt, params=None):
        return None

    async def flush(self):
        pass


def test_exclude_tag_core_inserts_tombstone_and_recomputes_target(monkeypatch):
    from oddish.core import tags_core

    enqueued: list[dict] = []

    async def _fake_enqueue(s, *, scope, target_id, task_id, org_id, mode="direct"):
        enqueued.append({"scope": scope, "target_id": target_id, "mode": mode})

    async def _fake_recompute(s, *, task_id):
        return None

    monkeypatch.setattr(tags_core, "enqueue_tag_project_worker_job", _fake_enqueue)
    monkeypatch.setattr(tags_core, "recompute_task_browse_projection", _fake_recompute)

    session = _FakeSession()
    _run(
        tags_core.exclude_tag_core(
            session,
            tag_id="tag-1",
            experiment_id="exp-1",
            scope="TASK",
            target_id="task-1",
            org_id="org-1",
            actor_user_id="u-1",
        )
    )
    assert any("INSERT INTO tag_exclusions" in s for s, _ in session.executed)
    assert enqueued and enqueued[0]["mode"] == "task_all_versions"


def test_unexclude_tag_core_soft_deletes_tombstone_and_recomputes(monkeypatch):
    from oddish.core import tags_core

    async def _fake_enqueue(s, **kwargs):
        return None

    async def _fake_recompute(s, *, task_id):
        return None

    monkeypatch.setattr(tags_core, "enqueue_tag_project_worker_job", _fake_enqueue)
    monkeypatch.setattr(tags_core, "recompute_task_browse_projection", _fake_recompute)

    session = _FakeSession()
    _run(
        tags_core.unexclude_tag_core(
            session,
            tag_id="tag-1",
            experiment_id="exp-1",
            scope="VERSION",
            target_id="task-1-v3",
            task_id="task-1",
            org_id="org-1",
            actor_user_id="u-1",
        )
    )
    assert any("UPDATE tag_exclusions" in s for s, _ in session.executed)

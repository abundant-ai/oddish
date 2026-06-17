"""Apply / unapply / mode tests for tags_core.

Uses a thin in-memory fake session that captures every ``execute``,
``add``, and ``flush`` call. Real SQL execution is exercised in DB
integration tests, not here.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _run(coro):
    return asyncio.run(coro)


class _StubResult:
    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def all(self):
        return self._rows

    def scalar(self):
        return self._scalar

    @property
    def rowcount(self):
        return len(self._rows) if self._rows else 0


class _FakeSession:
    def __init__(self, scalar_for_tag=None):
        self.scalar_for_tag = scalar_for_tag
        self.added: list = []
        self.executed: list[tuple[str, dict]] = []

    def add(self, row):
        self.added.append(row)

    async def execute(self, stmt, params=None):
        self.executed.append((str(stmt), dict(params or {})))
        return _StubResult()

    async def scalar(self, stmt, params=None):
        self.executed.append((str(stmt), dict(params or {})))
        return self.scalar_for_tag

    async def flush(self):
        pass


def test_assign_tag_core_writes_assignment_and_enqueues_projection(monkeypatch):
    from oddish.core import tags_core

    session = _FakeSession()

    enqueued: list[dict] = []

    async def _fake_enqueue(s, *, scope, target_id, task_id, org_id, mode="direct"):
        enqueued.append({"scope": scope, "target_id": target_id, "mode": mode})

    monkeypatch.setattr(tags_core, "enqueue_tag_project_worker_job", _fake_enqueue)

    async def _fake_recompute(s, *, task_id):
        pass

    monkeypatch.setattr(tags_core, "recompute_task_browse_projection", _fake_recompute)

    _run(
        tags_core.assign_tag_core(
            session,
            tag_id="tag-1",
            scope="TASK",
            target_id="task-1",
            task_id="task-1",
            org_id="org-1",
            actor_user_id="u-1",
            source="DIRECT",
        )
    )
    assert any("INSERT INTO tag_assignments" in s for s, _ in session.executed)
    assert any("INSERT INTO tag_events" in s for s, _ in session.executed)
    assert enqueued and enqueued[0]["scope"] == "TASK"


def test_unassign_tag_core_emits_remove_event_and_enqueues_projection(monkeypatch):
    from oddish.core import tags_core

    session = _FakeSession()
    enqueued: list[dict] = []

    async def _fake_enqueue(s, *, scope, target_id, task_id, org_id, mode="direct"):
        enqueued.append({"scope": scope, "mode": mode})

    monkeypatch.setattr(tags_core, "enqueue_tag_project_worker_job", _fake_enqueue)

    async def _fake_recompute(s, *, task_id):
        pass

    monkeypatch.setattr(tags_core, "recompute_task_browse_projection", _fake_recompute)

    _run(
        tags_core.unassign_tag_core(
            session,
            tag_id="tag-1",
            scope="TASK",
            target_id="task-1",
            task_id="task-1",
            org_id="org-1",
            actor_user_id="u-1",
        )
    )
    assert any("UPDATE tag_assignments" in s for s, _ in session.executed)
    assert any("INSERT INTO tag_events" in s for s, _ in session.executed)
    assert enqueued and enqueued[0]["scope"] == "TASK"


def test_apply_snapshot_experiment_tag_materializes_to_each_member(monkeypatch):
    from oddish.core import tags_core

    session = _FakeSession()
    member_ids = ["task-a", "task-b", "task-c"]

    async def _fake_members(s, experiment_id):
        return list(member_ids)

    monkeypatch.setattr(tags_core, "_list_experiment_member_task_ids", _fake_members)

    async def _fake_assign(s, **kwargs):
        session.added.append(("assign", kwargs))
        return "id-" + kwargs["target_id"]

    monkeypatch.setattr(tags_core, "assign_tag_core", _fake_assign)

    _run(
        tags_core.apply_experiment_tag_core(
            session,
            tag_id="tag-1",
            experiment_id="exp-1",
            mode="snapshot",
            org_id="org-1",
            actor_user_id="u-1",
        )
    )
    assigns = [k for kind, k in session.added if kind == "assign"]
    assert {a["target_id"] for a in assigns} == set(member_ids)
    for a in assigns:
        assert a["source"] == "EXPERIMENT_SNAPSHOT"
        assert a["source_experiment_id"] == "exp-1"


def test_apply_living_experiment_tag_writes_single_experiment_row(monkeypatch):
    from oddish.core import tags_core

    session = _FakeSession()

    async def _fake_assign(s, **kwargs):
        session.added.append(("assign", kwargs))
        return "exp-assign"

    monkeypatch.setattr(tags_core, "assign_tag_core", _fake_assign)

    _run(
        tags_core.apply_experiment_tag_core(
            session,
            tag_id="tag-1",
            experiment_id="exp-1",
            mode="living",
            org_id="org-1",
            actor_user_id="u-1",
        )
    )
    assigns = [k for kind, k in session.added if kind == "assign"]
    assert len(assigns) == 1
    assert assigns[0]["scope"] == "EXPERIMENT"
    assert assigns[0]["target_id"] == "exp-1"
    assert assigns[0]["source"] == "EXPERIMENT_LIVING"

"""delete_tag_core cascade semantics — the dashboard delete path.

The chip-editor delete always targets an assigned tag, so the router must
request ``cascade_remove_assignments``; without it the core refuses (safety
default for raw API callers).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _run(coro):
    return asyncio.run(coro)


class _StubResult:
    def __init__(self, rows=None):
        self._rows = rows or []

    def all(self):
        return self._rows

    @property
    def rowcount(self):
        return 1


class _FakeSession:
    """Scripted session: scalar() feeds the ACTIVE-assignments count,
    execute() returns queued results (the cascade UPDATE..RETURNING)."""

    def __init__(self, *, active_count=0, execute_results=None):
        self.active_count = active_count
        self.execute_results = list(execute_results or [])
        self.statements: list[str] = []

    async def scalar(self, stmt, params=None):
        self.statements.append(str(stmt))
        return self.active_count

    async def execute(self, stmt, params=None):
        self.statements.append(str(stmt))
        if self.execute_results:
            return self.execute_results.pop(0)
        return _StubResult()

    async def flush(self):
        pass


def test_delete_without_cascade_rejects_assigned_tag():
    from oddish.core.tags import service as tags_core

    session = _FakeSession(active_count=3)
    try:
        _run(
            tags_core.delete_tag_core(
                session,
                tag_id="tg1",
                org_id="org1",
                actor_user_id="u1",
                expected_row_version=1,
            )
        )
        raise AssertionError("expected TagPolicyError")
    except tags_core.TagPolicyError as exc:
        assert "3 ACTIVE assignments" in str(exc)
    # Refused before any state mutation: only the COUNT ran.
    assert len(session.statements) == 1
    assert "COUNT(*)" in session.statements[0]


def test_delete_with_cascade_removes_assignments_and_reprojects(monkeypatch):
    from oddish.core.tags import service as tags_core

    state_updates: list[dict] = []
    events: list[dict] = []
    enqueues: list[dict] = []
    recomputes: list[str] = []

    async def fake_state_update(session, **kw):
        state_updates.append(kw)

    async def fake_emit(session, **kw):
        events.append(kw)

    async def fake_enqueue(session, **kw):
        enqueues.append(kw)

    async def fake_recompute(session, *, task_id):
        recomputes.append(task_id)

    monkeypatch.setattr(tags_core, "_optimistic_state_update", fake_state_update)
    monkeypatch.setattr(tags_core, "_emit_tag_event", fake_emit)
    monkeypatch.setattr(tags_core, "enqueue_tag_project_worker_job", fake_enqueue)
    monkeypatch.setattr(tags_core, "recompute_task_browse_projection", fake_recompute)

    # Two ACTIVE assignments flip: one task-scoped, one living-experiment.
    returning = _StubResult(
        rows=[
            ("a1", "task9", "TASK", "task9"),
            ("a2", None, "EXPERIMENT", "exp7"),
        ]
    )
    session = _FakeSession(execute_results=[returning])

    _run(
        tags_core.delete_tag_core(
            session,
            tag_id="tg1",
            org_id="org1",
            actor_user_id="u1",
            expected_row_version=4,
            cascade_remove_assignments=True,
        )
    )

    # No COUNT guard on the cascade path; the UPDATE..RETURNING ran.
    assert any("UPDATE tag_assignments" in s for s in session.statements)
    assert state_updates and state_updates[0]["new_state"] == "DELETED"
    assert state_updates[0]["expected_row_version"] == 4

    # Per-assignment REMOVE events + the final DELETE event.
    actions = [e["action"] for e in events]
    assert actions.count("REMOVE") == 2
    assert actions[-1] == "DELETE"

    # Experiment assignment fans out; task assignment reprojects directly.
    exp_jobs = [e for e in enqueues if e["scope"] == "EXPERIMENT"]
    task_jobs = [e for e in enqueues if e["scope"] == "TASK"]
    assert exp_jobs and exp_jobs[0]["target_id"] == "exp7"
    assert exp_jobs[0]["mode"] == "experiment_living_fanout"
    assert task_jobs and task_jobs[0]["target_id"] == "task9"
    assert task_jobs[0]["mode"] == "task_all_versions"
    assert recomputes == ["task9"]

"""Tests for the TAG_PROJECT enqueue helper.

The helper inserts via a raw ``INSERT ... ON CONFLICT`` (DB-level
coalescing), so these tests monkeypatch the low-level
``_insert_tag_project_with_coalescing`` and assert on the params it
receives. No real DB needed.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class _FakeSession:
    def __init__(self):
        self.added: list = []
        self.flush_count = 0

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        self.flush_count += 1


def _run(coro):
    return asyncio.run(coro)


def test_enqueue_tag_project_writes_a_worker_jobs_row(monkeypatch):
    from oddish.core import tags_enqueue

    captured: list[dict] = []

    async def _fake_insert(
        session, *, kind, queue_key, payload, subject_table, subject_id, org_id
    ):
        captured.append(
            {
                "kind": kind,
                "queue_key": queue_key,
                "payload": payload,
                "subject_table": subject_table,
                "subject_id": subject_id,
                "org_id": org_id,
            }
        )
        return {"id": "wj-1", "inserted": True}

    monkeypatch.setattr(
        tags_enqueue, "_insert_tag_project_with_coalescing", _fake_insert
    )
    session = _FakeSession()
    _run(
        tags_enqueue.enqueue_tag_project_worker_job(
            session, scope="TASK", target_id="task-1", task_id="task-1", org_id="org-1"
        )
    )
    assert len(captured) == 1
    row = captured[0]
    assert row["kind"] == "TAG_PROJECT"
    assert row["queue_key"] == "tag-project"
    assert row["subject_table"] == "tasks"
    assert row["subject_id"] == "task-1"
    assert row["payload"]["scope"] == "TASK"
    assert row["payload"]["target_id"] == "task-1"
    assert row["org_id"] == "org-1"


def test_enqueue_tag_project_uses_tasks_queue_key(monkeypatch):
    from oddish.core import tags_enqueue

    captured: list[dict] = []

    async def _fake_insert(
        session, *, kind, queue_key, payload, subject_table, subject_id, org_id
    ):
        captured.append({"queue_key": queue_key})
        return {"id": "wj-1", "inserted": True}

    monkeypatch.setattr(
        tags_enqueue, "_insert_tag_project_with_coalescing", _fake_insert
    )
    session = _FakeSession()
    _run(
        tags_enqueue.enqueue_tag_project_worker_job(
            session,
            scope="VERSION",
            target_id="task-1-v3",
            task_id="task-1",
            org_id="org-1",
        )
    )
    assert captured[0]["queue_key"] == "tag-project"


def test_enqueue_tag_project_coalesces_against_active_jobs(monkeypatch):
    from oddish.core import tags_enqueue

    inserted: list[dict] = []

    async def _fake_insert_on_conflict(
        session, *, kind, queue_key, payload, subject_table, subject_id, org_id
    ):
        if any(
            r["subject_table"] == subject_table and r["subject_id"] == subject_id
            for r in inserted
        ):
            return None
        inserted.append(
            {
                "kind": kind,
                "queue_key": queue_key,
                "payload": payload,
                "subject_table": subject_table,
                "subject_id": subject_id,
                "org_id": org_id,
            }
        )
        return {"inserted": True}

    monkeypatch.setattr(
        tags_enqueue, "_insert_tag_project_with_coalescing", _fake_insert_on_conflict
    )
    session = _FakeSession()
    _run(
        tags_enqueue.enqueue_tag_project_worker_job(
            session, scope="TASK", target_id="task-1", task_id="task-1", org_id="org-1"
        )
    )
    _run(
        tags_enqueue.enqueue_tag_project_worker_job(
            session, scope="TASK", target_id="task-1", task_id="task-1", org_id="org-1"
        )
    )
    assert len(inserted) == 1


def test_enqueue_tag_project_experiment_living_fanout_payload(monkeypatch):
    from oddish.core import tags_enqueue

    captured: list[dict] = []

    async def _fake_insert(
        session, *, kind, queue_key, payload, subject_table, subject_id, org_id
    ):
        captured.append(
            {
                "payload": payload,
                "subject_table": subject_table,
                "subject_id": subject_id,
            }
        )
        return {"id": "wj-1", "inserted": True}

    monkeypatch.setattr(
        tags_enqueue, "_insert_tag_project_with_coalescing", _fake_insert
    )
    session = _FakeSession()
    _run(
        tags_enqueue.enqueue_tag_project_worker_job(
            session,
            scope="EXPERIMENT",
            target_id="exp-1",
            task_id=None,
            org_id="org-1",
            mode="experiment_living_fanout",
        )
    )
    row = captured[0]
    assert row["payload"]["mode"] == "experiment_living_fanout"
    assert row["payload"]["scope"] == "EXPERIMENT"
    assert row["payload"]["target_id"] == "exp-1"
    assert row["subject_table"] == "experiments"
    assert row["subject_id"] == "exp-1"


def test_enqueue_tag_project_for_new_version_uses_version_scope(monkeypatch):
    from oddish.core import tasks as tasks_core

    enqueued: list[dict] = []

    async def _fake_enqueue(
        session, *, scope, target_id, task_id, org_id, mode="direct"
    ):
        enqueued.append(
            {
                "scope": scope,
                "target_id": target_id,
                "task_id": task_id,
                "org_id": org_id,
                "mode": mode,
            }
        )

    monkeypatch.setattr(tasks_core, "enqueue_tag_project_worker_job", _fake_enqueue)
    _run(
        tasks_core._enqueue_tag_project_for_new_version(
            session=None, task_id="t-1", version_id="t-1-v3", org_id="org-1"
        )
    )
    assert len(enqueued) == 1
    e = enqueued[0]
    assert e["scope"] == "VERSION"
    assert e["target_id"] == "t-1-v3"
    assert e["task_id"] == "t-1"
    assert e["org_id"] == "org-1"


def test_recompute_tag_projection_on_membership_change_does_sync_recompute_and_async_enqueue(
    monkeypatch,
):
    from oddish import queue as queue_module

    recomputed: list[str] = []
    enqueued: list[dict] = []

    async def _fake_recompute(session, *, task_id):
        recomputed.append(task_id)

    async def _fake_enqueue(
        session, *, scope, target_id, task_id, org_id, mode="direct"
    ):
        enqueued.append(
            {
                "scope": scope,
                "target_id": target_id,
                "task_id": task_id,
                "org_id": org_id,
                "mode": mode,
            }
        )

    monkeypatch.setattr(
        queue_module, "recompute_task_browse_projection", _fake_recompute
    )
    monkeypatch.setattr(queue_module, "enqueue_tag_project_worker_job", _fake_enqueue)

    _run(
        queue_module._recompute_tag_projection_on_membership_change(
            session=None, task_id="t-1", experiment_id="e-1", org_id="org-1"
        )
    )

    assert recomputed == ["t-1"]
    assert len(enqueued) == 1
    e = enqueued[0]
    assert e["scope"] == "TASK"
    assert e["target_id"] == "t-1"
    assert e["task_id"] == "t-1"
    assert e["mode"] == "task_all_versions"
    assert e["org_id"] == "org-1"

"""Tests for ``oddish.workers.queue.tag_project_handler.run_tag_project_job``."""
from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class _FakeSession:
    def __init__(self):
        self.called: list[tuple[str, dict]] = []
        self.committed = False

    async def execute(self, stmt, params=None):
        self.called.append((str(stmt), dict(params or {})))

        class _R:
            def all(self_inner):
                return []

            def scalar(self_inner):
                return None

        return _R()

    async def scalar(self, stmt, params=None):
        self.called.append((str(stmt), dict(params or {})))
        return None

    async def flush(self):
        pass

    async def commit(self):
        self.committed = True


@asynccontextmanager
async def _fake_get_session_factory(session):
    yield session


def _run(coro):
    return asyncio.run(coro)


def test_run_tag_project_job_direct_task_calls_recompute(monkeypatch):
    from oddish.workers.queue import tag_project_handler

    calls: list[tuple[str, str]] = []

    async def _fake_recompute_task(session, *, task_id):
        calls.append(("task", task_id))

    async def _fake_recompute_all_versions(session, *, task_id, chunk_size=200):
        calls.append(("all_versions", task_id))
        return 0

    monkeypatch.setattr(
        tag_project_handler,
        "recompute_task_browse_projection",
        _fake_recompute_task,
    )
    monkeypatch.setattr(
        tag_project_handler,
        "recompute_all_versions_for_task",
        _fake_recompute_all_versions,
    )

    session = _FakeSession()

    @asynccontextmanager
    async def _get_session():
        yield session

    monkeypatch.setattr(tag_project_handler, "get_session", _get_session)

    summary = _run(
        tag_project_handler.run_tag_project_job(
            payload={"scope": "TASK", "target_id": "t-1", "task_id": "t-1", "mode": "direct"}
        )
    )
    assert ("task", "t-1") in calls
    assert summary["task_id"] == "t-1"


def test_run_tag_project_job_experiment_fanout_chunks_members(monkeypatch):
    from oddish.workers.queue import tag_project_handler

    seen_tasks: list[str] = []

    async def _fake_recompute_task(session, *, task_id):
        seen_tasks.append(task_id)

    async def _fake_recompute_all_versions(session, *, task_id, chunk_size=200):
        return 0

    async def _fake_members(session, experiment_id, *, after_task_id=None, limit):
        all_ids = [f"t-{i}" for i in range(5)]
        if after_task_id is not None:
            all_ids = [t for t in all_ids if t > after_task_id]
        return all_ids[:limit]

    monkeypatch.setattr(
        tag_project_handler,
        "recompute_task_browse_projection",
        _fake_recompute_task,
    )
    monkeypatch.setattr(
        tag_project_handler,
        "recompute_all_versions_for_task",
        _fake_recompute_all_versions,
    )
    monkeypatch.setattr(
        tag_project_handler,
        "_list_experiment_member_task_ids",
        _fake_members,
    )

    enqueued: list[dict] = []

    async def _fake_enqueue(session, **kwargs):
        enqueued.append(kwargs)

    monkeypatch.setattr(
        tag_project_handler, "enqueue_tag_project_worker_job", _fake_enqueue
    )

    async def _fake_heartbeat(session, *, target_id, batch_size, progress):
        return None

    monkeypatch.setattr(
        tag_project_handler, "_heartbeat_progress", _fake_heartbeat
    )

    session = _FakeSession()

    @asynccontextmanager
    async def _get_session():
        yield session

    monkeypatch.setattr(tag_project_handler, "get_session", _get_session)

    summary = _run(
        tag_project_handler.run_tag_project_job(
            payload={
                "scope": "EXPERIMENT",
                "target_id": "exp-1",
                "task_id": None,
                "mode": "experiment_living_fanout",
            }
        )
    )
    assert seen_tasks == ["t-0", "t-1", "t-2", "t-3", "t-4"]
    assert summary["tasks_recomputed"] == 5
    assert summary["continuation_enqueued"] is False
    assert enqueued == []


def test_run_tag_project_job_experiment_fanout_enqueues_continuation(monkeypatch):
    """When member count exceeds the per-job chunk size we process one
    bounded batch, persist a cursor, and enqueue a continuation
    TAG_PROJECT job. The cursor is the last processed task_id and the
    continuation's payload picks up after it.
    """
    from oddish.workers.queue import tag_project_handler

    seen_tasks: list[str] = []

    async def _fake_recompute_task(session, *, task_id):
        seen_tasks.append(task_id)

    async def _fake_recompute_all_versions(session, *, task_id, chunk_size=200):
        return 0

    all_ids = [f"t-{i:03d}" for i in range(120)]

    async def _fake_members(session, experiment_id, *, after_task_id=None, limit):
        ids = all_ids
        if after_task_id is not None:
            ids = [t for t in ids if t > after_task_id]
        return ids[:limit]

    monkeypatch.setattr(
        tag_project_handler, "recompute_task_browse_projection", _fake_recompute_task
    )
    monkeypatch.setattr(
        tag_project_handler,
        "recompute_all_versions_for_task",
        _fake_recompute_all_versions,
    )
    monkeypatch.setattr(
        tag_project_handler, "_list_experiment_member_task_ids", _fake_members
    )

    enqueued: list[dict] = []

    async def _fake_enqueue(session, **kwargs):
        enqueued.append(kwargs)

    monkeypatch.setattr(
        tag_project_handler, "enqueue_tag_project_worker_job", _fake_enqueue
    )

    heartbeats: list[dict] = []

    async def _fake_heartbeat(session, *, target_id, batch_size, progress):
        heartbeats.append({"target_id": target_id, "progress": progress})

    monkeypatch.setattr(
        tag_project_handler, "_heartbeat_progress", _fake_heartbeat
    )

    session = _FakeSession()

    @asynccontextmanager
    async def _get_session():
        yield session

    monkeypatch.setattr(tag_project_handler, "get_session", _get_session)

    summary = _run(
        tag_project_handler.run_tag_project_job(
            payload={
                "scope": "EXPERIMENT",
                "target_id": "exp-1",
                "task_id": None,
                "mode": "experiment_living_fanout",
                "after_task_id": None,
            }
        )
    )
    assert summary["tasks_recomputed"] == 50
    assert summary["continuation_enqueued"] is True
    assert summary["next_cursor"] == "t-049"
    assert len(enqueued) == 1
    assert enqueued[0]["scope"] == "EXPERIMENT"
    assert enqueued[0]["target_id"] == "exp-1"
    assert enqueued[0]["mode"] == "experiment_living_fanout"
    assert enqueued[0]["after_task_id"] == "t-049"
    assert heartbeats and heartbeats[-1]["target_id"] == "exp-1"

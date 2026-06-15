"""Tests for the un-link invalidation hook."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _run(coro):
    return asyncio.run(coro)


def test_unlink_invalidation_hook_present():
    from oddish import queue as queue_module

    assert hasattr(queue_module, "_recompute_tag_projection_on_membership_removed")


def test_unlink_hook_recomputes_and_enqueues_task_all_versions(monkeypatch):
    from oddish import queue as queue_module

    recomputed: list[str] = []
    enqueued: list[dict] = []

    async def _fake_recompute(session, *, task_id):
        recomputed.append(task_id)

    async def _fake_enqueue(
        session, *, scope, target_id, task_id, org_id, mode="direct"
    ):
        enqueued.append(
            {"scope": scope, "target_id": target_id, "task_id": task_id, "mode": mode}
        )

    monkeypatch.setattr(
        queue_module, "recompute_task_browse_projection", _fake_recompute
    )
    monkeypatch.setattr(queue_module, "enqueue_tag_project_worker_job", _fake_enqueue)

    _run(
        queue_module._recompute_tag_projection_on_membership_removed(
            session=None, task_id="t-1", experiment_id="e-1", org_id="org-1"
        )
    )

    assert recomputed == ["t-1"]
    assert len(enqueued) == 1
    e = enqueued[0]
    assert e["scope"] == "TASK"
    assert e["target_id"] == "t-1"
    assert e["mode"] == "task_all_versions"

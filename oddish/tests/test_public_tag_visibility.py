"""Tests for public-endpoint tag visibility filtering."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
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
    from oddish.core.sharing import public as public_module

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


def test_public_task_response_drops_private_fields_and_metadata():
    from oddish.core.sharing.public_projection import public_task_github_meta
    from oddish.schemas import PublicTaskStatusResponse

    now = datetime.now(UTC)
    source = {
        "id": "task-1",
        "name": "Public task",
        "status": "completed",
        "priority": "low",
        "user": "private-owner",
        "github_username": "private-owner",
        "github_meta": {
            "category": "JS",
            "github_username": "private-owner",
            "repository": "private/repository",
        },
        "link": "https://secret.example/task",
        "task_path": "tasks/public-task",
        "experiment_id": "experiment-1",
        "experiment_name": "Public experiment",
        "experiment_owner": "private-owner",
        "experiment_link": "https://secret.example/experiment",
        "total": 0,
        "completed": 0,
        "failed": 0,
        "progress": "0/0 completed",
        "jobs": [{"id": "private-worker-job"}],
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
    }

    response = PublicTaskStatusResponse.model_validate(source)
    response.github_meta = public_task_github_meta(response.github_meta)
    payload = response.model_dump()

    for private_field in (
        "user",
        "github_username",
        "link",
        "experiment_owner",
        "experiment_link",
        "jobs",
    ):
        assert private_field not in payload
    assert payload["github_meta"] == {"category": "JS"}
    assert "private-owner" not in response.model_dump_json()
    assert "private/repository" not in response.model_dump_json()

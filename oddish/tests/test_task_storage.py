from __future__ import annotations

from pathlib import Path
import sys

from fastapi import HTTPException
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.api import tasks as tasks_api
from oddish.config import settings


class _FakeStorage:
    def __init__(self, *, exists: bool):
        self.exists = exists
        self.prefix_exists_calls: list[str] = []
        self.list_keys_called = False

    async def prefix_exists(self, prefix: str) -> bool:
        self.prefix_exists_calls.append(prefix)
        return self.exists

    async def list_keys(self, prefix: str) -> list[str]:
        self.list_keys_called = True
        raise AssertionError("resolve_task_storage should not call list_keys")


@pytest.mark.asyncio
async def test_resolve_task_storage_uses_prefix_probe_for_s3(monkeypatch):
    storage = _FakeStorage(exists=True)
    monkeypatch.setattr(settings, "s3_enabled", True)
    monkeypatch.setattr(tasks_api, "get_storage_client", lambda: storage)

    task_path, task_s3_key = await tasks_api.resolve_task_storage("task-123")

    assert task_path == "s3://tasks/task-123/"
    assert task_s3_key == "tasks/task-123/"
    assert storage.prefix_exists_calls == ["tasks/task-123/"]
    assert storage.list_keys_called is False


@pytest.mark.asyncio
async def test_resolve_task_storage_raises_404_when_prefix_missing(monkeypatch):
    storage = _FakeStorage(exists=False)
    monkeypatch.setattr(settings, "s3_enabled", True)
    monkeypatch.setattr(tasks_api, "get_storage_client", lambda: storage)

    with pytest.raises(HTTPException, match="Task task-404 not found in S3") as exc_info:
        await tasks_api.resolve_task_storage("task-404")

    assert exc_info.value.status_code == 404
    assert storage.prefix_exists_calls == ["tasks/task-404/"]

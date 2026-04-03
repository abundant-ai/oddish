from __future__ import annotations

from pathlib import Path
import sys

from fastapi import HTTPException
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.api import tasks as tasks_api
from oddish.config import settings
from oddish.db import storage as storage_mod


class _FakeStorage:
    def __init__(self, *, exists: bool):
        self.exists = exists
        self.prefix_exists_calls: list[str] = []
        self.list_keys_called = False
        self.download_task_directory_calls: list[tuple[str, Path]] = []

    async def prefix_exists(self, prefix: str) -> bool:
        self.prefix_exists_calls.append(prefix)
        return self.exists

    async def list_keys(self, prefix: str) -> list[str]:
        self.list_keys_called = True
        raise AssertionError("resolve_task_storage should not call list_keys")

    async def download_task_directory(self, s3_prefix: str, local_path: Path) -> None:
        self.download_task_directory_calls.append((s3_prefix, local_path))
        local_path.mkdir(parents=True, exist_ok=True)
        (local_path / "task.toml").write_text("name = 'demo'\n")


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


def test_resolve_mounted_task_directory_prefers_worker_mount(monkeypatch, tmp_path):
    mounted_root = tmp_path / "mounted-tasks"
    task_dir = mounted_root / "task-123"
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text("name = 'demo'\n")

    monkeypatch.setattr(storage_mod, "WORKER_TASK_MOUNT_PATH", mounted_root)
    monkeypatch.setattr(storage_mod, "WORKER_TASK_KEY_PREFIX", "tasks/")

    resolved = storage_mod.resolve_mounted_task_directory("tasks/task-123/")

    assert resolved == task_dir


@pytest.mark.asyncio
async def test_resolve_task_directory_falls_back_to_download_when_mount_missing(
    monkeypatch, tmp_path
):
    storage = _FakeStorage(exists=True)
    monkeypatch.setattr(
        storage_mod, "WORKER_TASK_MOUNT_PATH", tmp_path / "missing-mount"
    )
    monkeypatch.setattr(storage_mod, "WORKER_TASK_KEY_PREFIX", "tasks/")
    monkeypatch.setattr(storage_mod, "get_storage_client", lambda: storage)

    task_dir, temp_dir, resolved_s3_key = await storage_mod.resolve_task_directory(
        "task-123",
        task_s3_key="tasks/task-123/",
        task_path=None,
    )

    assert resolved_s3_key == "tasks/task-123/"
    assert temp_dir == task_dir
    assert task_dir.exists()
    assert storage.download_task_directory_calls

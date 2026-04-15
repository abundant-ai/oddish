from __future__ import annotations

import io
from pathlib import Path
import sys
import tarfile

from fastapi import HTTPException
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core import tasks as tasks_api
from oddish.config import settings
from oddish.db import storage as storage_mod


def _make_task_archive(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class _FakeStorage:
    """Fake storage for resolve_task_storage tests.

    *object_exists_keys* controls which specific S3 keys ``object_exists``
    returns ``True`` for.  *prefix_exists_result* controls the final
    ``prefix_exists`` fallback.
    """

    def __init__(
        self,
        *,
        exists: bool = True,
        object_exists_keys: set[str] | None = None,
        list_keys_result: list[str] | None = None,
    ):
        self.exists = exists
        self.object_exists_keys: set[str] = object_exists_keys or set()
        self.list_keys_result: list[str] = list_keys_result or []
        self.prefix_exists_calls: list[str] = []
        self.object_exists_calls: list[str] = []
        self.list_keys_calls: list[str] = []
        self.download_task_directory_calls: list[tuple[str, Path]] = []

    async def object_exists(self, s3_key: str) -> bool:
        self.object_exists_calls.append(s3_key)
        return s3_key in self.object_exists_keys

    async def prefix_exists(self, prefix: str) -> bool:
        self.prefix_exists_calls.append(prefix)
        return self.exists

    async def list_keys(self, prefix: str) -> list[str]:
        self.list_keys_calls.append(prefix)
        return self.list_keys_result

    async def download_task_directory(self, s3_prefix: str, local_path: Path) -> None:
        self.download_task_directory_calls.append((s3_prefix, local_path))
        local_path.mkdir(parents=True, exist_ok=True)
        (local_path / "task.toml").write_text("name = 'demo'\n")


class _FakePaginator:
    def __init__(self, pages: list[dict]):
        self.pages = pages

    async def paginate(self, **_: object):
        for page in self.pages:
            yield page


class _FakeS3Client:
    def __init__(self, pages: list[dict] | None = None):
        self.pages = pages or []
        self.delete_calls: list[dict] = []

    def get_paginator(self, operation_name: str) -> _FakePaginator:
        assert operation_name == "list_objects_v2"
        return _FakePaginator(self.pages)

    async def delete_objects(self, **kwargs: object) -> dict:
        self.delete_calls.append(kwargs)
        return {"Deleted": kwargs["Delete"]["Objects"]}


class _FakeDeleteStorage:
    def __init__(self, *, deleted: int):
        self.deleted = deleted
        self.delete_prefixes_calls: list[list[str]] = []

    async def delete_prefixes(self, prefixes: list[str]) -> int:
        self.delete_prefixes_calls.append(prefixes)
        return self.deleted


@pytest.mark.asyncio
async def test_resolve_task_storage_returns_root_when_root_archive_exists(monkeypatch):
    """When the archive is at the unversioned root, return the root prefix."""
    storage = _FakeStorage(
        object_exists_keys={"tasks/task-123/.oddish-task.tar.gz"},
    )
    monkeypatch.setattr(tasks_api, "get_storage_client", lambda: storage)

    task_path, task_s3_key = await tasks_api.resolve_task_storage("task-123")

    assert task_path == "s3://tasks/task-123/"
    assert task_s3_key == "tasks/task-123/"
    assert "tasks/task-123/.oddish-task.tar.gz" in storage.object_exists_calls


@pytest.mark.asyncio
async def test_resolve_task_storage_finds_versioned_archive(monkeypatch):
    """When the archive only exists at a versioned sub-prefix (init/complete
    upload path), resolve_task_storage should return the versioned prefix."""
    storage = _FakeStorage(
        exists=True,
        list_keys_result=["tasks/task-123/v1/.oddish-task.tar.gz"],
    )
    monkeypatch.setattr(tasks_api, "get_storage_client", lambda: storage)

    task_path, task_s3_key = await tasks_api.resolve_task_storage("task-123")

    assert task_path == "s3://tasks/task-123/v1/"
    assert task_s3_key == "tasks/task-123/v1/"


@pytest.mark.asyncio
async def test_resolve_task_storage_picks_latest_versioned_archive(monkeypatch):
    """When multiple versioned archives exist, the latest version wins."""
    storage = _FakeStorage(
        exists=True,
        list_keys_result=[
            "tasks/task-123/v1/.oddish-task.tar.gz",
            "tasks/task-123/v2/.oddish-task.tar.gz",
        ],
    )
    monkeypatch.setattr(tasks_api, "get_storage_client", lambda: storage)

    task_path, task_s3_key = await tasks_api.resolve_task_storage("task-123")

    assert task_path == "s3://tasks/task-123/v2/"
    assert task_s3_key == "tasks/task-123/v2/"


@pytest.mark.asyncio
async def test_resolve_task_storage_raises_404_when_prefix_missing(monkeypatch):
    storage = _FakeStorage(exists=False, list_keys_result=[])
    monkeypatch.setattr(tasks_api, "get_storage_client", lambda: storage)

    with pytest.raises(
        HTTPException, match="Task task-404 not found in S3"
    ) as exc_info:
        await tasks_api.resolve_task_storage("task-404")

    assert exc_info.value.status_code == 404


def test_resolve_mounted_task_directory_prefers_worker_mount(monkeypatch, tmp_path):
    mounted_root = tmp_path / "mounted-tasks"
    task_dir = mounted_root / "task-123"
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text("name = 'demo'\n")

    monkeypatch.setattr(storage_mod, "WORKER_TASK_MOUNT_PATH", mounted_root)
    monkeypatch.setattr(storage_mod, "WORKER_TASK_KEY_PREFIX", "tasks/")

    resolved = storage_mod.resolve_mounted_task_directory("tasks/task-123/")

    assert resolved == task_dir


def test_resolve_mounted_task_directory_skips_archive_only_mount(monkeypatch, tmp_path):
    mounted_root = tmp_path / "mounted-tasks"
    task_dir = mounted_root / "task-123"
    task_dir.mkdir(parents=True)
    (task_dir / storage_mod.StorageClient._TASK_ARCHIVE_OBJECT_NAME).write_bytes(
        b"archive"
    )

    monkeypatch.setattr(storage_mod, "WORKER_TASK_MOUNT_PATH", mounted_root)
    monkeypatch.setattr(storage_mod, "WORKER_TASK_KEY_PREFIX", "tasks/")

    resolved = storage_mod.resolve_mounted_task_directory("tasks/task-123/")

    assert resolved is None


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


@pytest.mark.asyncio
async def test_download_task_directory_extracts_archive_object(monkeypatch, tmp_path):
    archive_bytes = _make_task_archive(
        {
            "task.toml": "name = 'demo'\n",
            "environment/run.sh": "#!/bin/sh\necho hi\n",
        }
    )
    storage = storage_mod.StorageClient()
    storage._client = object()

    async def fake_object_exists(s3_key: str) -> bool:
        assert s3_key == "tasks/task-123/.oddish-task.tar.gz"
        return True

    async def fake_download_bytes(s3_key: str) -> bytes:
        assert s3_key == "tasks/task-123/.oddish-task.tar.gz"
        return archive_bytes

    monkeypatch.setattr(storage, "object_exists", fake_object_exists)
    monkeypatch.setattr(storage, "download_bytes", fake_download_bytes)

    await storage.download_task_directory("tasks/task-123/", tmp_path)

    assert (tmp_path / "task.toml").read_text() == "name = 'demo'\n"
    assert (tmp_path / "environment" / "run.sh").read_text() == "#!/bin/sh\necho hi\n"


@pytest.mark.asyncio
async def test_download_task_directory_finds_versioned_archive(monkeypatch, tmp_path):
    """When the archive lives at a versioned sub-path (init/complete upload),
    download_task_directory should detect and extract it rather than
    downloading the tarball as a raw file."""
    archive_bytes = _make_task_archive(
        {
            "task.toml": "[agent]\ntimeout_sec = 1800\n",
            "instruction.md": "Do something\n",
        }
    )
    storage = storage_mod.StorageClient()
    storage._client = object()

    async def fake_object_exists(s3_key: str) -> bool:
        return False

    download_bytes_calls: list[str] = []

    async def fake_download_bytes(s3_key: str) -> bytes:
        download_bytes_calls.append(s3_key)
        return archive_bytes

    fake_pages = [
        {
            "Contents": [
                {"Key": "tasks/task-123/v1/.oddish-task.tar.gz"},
            ]
        }
    ]
    monkeypatch.setattr(storage, "object_exists", fake_object_exists)
    monkeypatch.setattr(storage, "download_bytes", fake_download_bytes)
    storage._client = _FakeS3Client(pages=fake_pages)
    monkeypatch.setattr(settings, "s3_bucket", "test-bucket")

    await storage.download_task_directory("tasks/task-123/", tmp_path)

    assert (tmp_path / "task.toml").exists()
    assert (tmp_path / "instruction.md").read_text() == "Do something\n"
    assert download_bytes_calls == ["tasks/task-123/v1/.oddish-task.tar.gz"]


@pytest.mark.asyncio
async def test_list_task_files_reads_archive_members(monkeypatch):
    archive_bytes = _make_task_archive(
        {
            "task.toml": "name = 'demo'\n",
            "environment/run.sh": "#!/bin/sh\necho hi\n",
        }
    )
    storage = storage_mod.StorageClient()
    storage._client = object()

    async def fake_object_exists(s3_key: str) -> bool:
        assert s3_key == "tasks/task-123/.oddish-task.tar.gz"
        return True

    async def fake_download_bytes(s3_key: str) -> bytes:
        assert s3_key == "tasks/task-123/.oddish-task.tar.gz"
        return archive_bytes

    monkeypatch.setattr(storage, "object_exists", fake_object_exists)
    monkeypatch.setattr(storage, "download_bytes", fake_download_bytes)

    listing = await storage.list_task_files(
        task_id="task-123",
        prefix=None,
        recursive=True,
        limit=1000,
        cursor=None,
        presign=False,
    )

    assert [entry["path"] for entry in listing["files"]] == [
        "environment/run.sh",
        "task.toml",
    ]
    assert listing["presigned"] is False


@pytest.mark.asyncio
async def test_list_task_files_presign_returns_archive_url(monkeypatch):
    archive_bytes = _make_task_archive({"task.toml": "name = 'demo'\n"})
    storage = storage_mod.StorageClient()
    storage._client = object()

    async def fake_object_exists(s3_key: str) -> bool:
        assert s3_key == "tasks/task-123/.oddish-task.tar.gz"
        return True

    async def fake_download_bytes(s3_key: str) -> bytes:
        assert s3_key == "tasks/task-123/.oddish-task.tar.gz"
        return archive_bytes

    async def fake_get_presigned_url(s3_key: str, expiration: int = 900) -> str:
        assert s3_key == "tasks/task-123/.oddish-task.tar.gz"
        assert expiration == 900
        return "https://example.com/task-archive"

    monkeypatch.setattr(storage, "object_exists", fake_object_exists)
    monkeypatch.setattr(storage, "download_bytes", fake_download_bytes)
    monkeypatch.setattr(storage, "get_presigned_url", fake_get_presigned_url)

    listing = await storage.list_task_files(
        task_id="task-123",
        prefix=None,
        recursive=True,
        limit=1000,
        cursor=None,
        presign=True,
    )

    assert listing["archive_url"] == "https://example.com/task-archive"
    assert listing["archive_key"] == "tasks/task-123/.oddish-task.tar.gz"
    assert listing["presigned"] is True


@pytest.mark.asyncio
async def test_get_task_file_content_reads_archive_member(monkeypatch):
    archive_bytes = _make_task_archive({"task.toml": "name = 'demo'\n"})
    storage = storage_mod.StorageClient()
    storage._client = object()

    async def fake_object_exists(s3_key: str) -> bool:
        assert s3_key == "tasks/task-123/.oddish-task.tar.gz"
        return True

    async def fake_download_bytes(s3_key: str) -> bytes:
        assert s3_key == "tasks/task-123/.oddish-task.tar.gz"
        return archive_bytes

    monkeypatch.setattr(storage, "object_exists", fake_object_exists)
    monkeypatch.setattr(storage, "download_bytes", fake_download_bytes)

    payload = await storage.get_task_file_content(
        task_id="task-123",
        file_path="task.toml",
        presign=False,
    )

    assert payload["content"] == "name = 'demo'\n"


@pytest.mark.asyncio
async def test_delete_prefix_deletes_all_matching_s3_objects(monkeypatch):
    fake_client = _FakeS3Client(
        pages=[
            {
                "Contents": [
                    {"Key": "tasks/task-123/task.toml"},
                    {"Key": "tasks/task-123/instruction.md"},
                ]
            }
        ]
    )
    storage = storage_mod.StorageClient()
    storage._client = fake_client
    monkeypatch.setattr(settings, "s3_bucket", "test-bucket")

    deleted = await storage.delete_prefix("tasks/task-123/")

    assert deleted == 2
    assert fake_client.delete_calls == [
        {
            "Bucket": "test-bucket",
            "Delete": {
                "Objects": [
                    {"Key": "tasks/task-123/task.toml"},
                    {"Key": "tasks/task-123/instruction.md"},
                ],
                "Quiet": True,
            },
        }
    ]


def test_collect_s3_prefixes_for_deletion_normalizes_and_dedupes():
    prefixes = storage_mod.collect_s3_prefixes_for_deletion(
        tasks=[
            ("tasks/task-123", None),
            (None, "s3://tasks/task-123/"),
            (None, "/tmp/local-task"),
        ],
        trials=[
            ("task-123-0", None),
            ("task-123-0", "tasks/task-123/trials/task-123-0/"),
            ("task-123-1", "tasks/task-123/trials/task-123-1"),
        ],
    )

    assert prefixes == [
        "tasks/task-123/",
        "tasks/task-123/trials/task-123-0/",
        "tasks/task-123/trials/task-123-1/",
    ]


@pytest.mark.asyncio
async def test_delete_s3_prefixes_skips_duplicates_and_empty_values(monkeypatch):
    storage = _FakeDeleteStorage(deleted=3)
    monkeypatch.setattr(storage_mod, "get_storage_client", lambda: storage)

    deleted = await storage_mod.delete_s3_prefixes(
        ["tasks/task-123/", "tasks/task-123/", ""]
    )

    assert deleted == 3
    assert storage.delete_prefixes_calls == [["tasks/task-123/"]]

"""Contract tests for exact task-version expansion manifests."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from oddish.core.endpoints import task_detail
from oddish.db.storage import StorageClient


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


@pytest.mark.asyncio
async def test_storage_reads_one_exact_key_and_strips_internal_fields(monkeypatch):
    key = "tasks/task-1/v3-files/.oddish-manifest.json"
    download = AsyncMock(
        return_value={
            "task_id": "task-1",
            "version": 3,
            "archive_key": "tasks/task-1/v3/.oddish-task.tar.gz",
            "archive_etag": '"secret-etag"',
            "source_prefix": "tasks/task-1/",
            "files": [
                {
                    "path": "verifier/check.py",
                    "size": 12,
                    "sha256": _digest(b"check"),
                    "source_key": "private/source/key",
                    "content": "do not expose",
                    "url": "https://storage.invalid/presigned",
                },
                {
                    "path": "large.bin",
                    "size": 2_000_000,
                    "skipped": True,
                    "skip_reason": "member_too_large",
                    "source_key": "private/large.bin",
                },
            ],
        }
    )
    storage = StorageClient()
    monkeypatch.setattr(storage, "download_json", download)

    result = await storage.read_task_version_manifest(
        task_id="task-1",
        version=3,
        manifest_key=key,
        task_s3_prefix="tasks/task-1/v3/",
        expansion_pending=False,
    )

    download.assert_awaited_once_with(key)
    assert result == {
        "status": "ready",
        "files": [
            {
                "path": "large.bin",
                "size": 2_000_000,
                "sha256": None,
                "skipped": True,
                "skip_reason": "member_too_large",
            },
            {
                "path": "verifier/check.py",
                "size": 12,
                "sha256": _digest(b"check"),
                "skipped": False,
                "skip_reason": None,
            },
        ],
    }
    public_json = json.dumps(result)
    assert "archive" not in public_json
    assert "source_key" not in public_json
    assert "content" not in public_json
    assert "presigned" not in public_json
    assert "storage.invalid" not in public_json


@pytest.mark.asyncio
async def test_storage_reports_pending_or_unavailable_without_path_probing(
    monkeypatch,
):
    download = AsyncMock(side_effect=AssertionError("must not read without pointer"))
    storage = StorageClient()
    monkeypatch.setattr(storage, "download_json", download)

    pending = await storage.read_task_version_manifest(
        task_id="task-1",
        version=3,
        manifest_key=None,
        task_s3_prefix="tasks/task-1/v3/",
        expansion_pending=True,
    )
    unavailable = await storage.read_task_version_manifest(
        task_id="task-1",
        version=3,
        manifest_key=None,
        task_s3_prefix="tasks/task-1/v3/",
        expansion_pending=False,
    )

    assert pending == {"status": "pending", "files": []}
    assert unavailable == {"status": "unavailable", "files": []}
    download.assert_not_awaited()


@pytest.mark.asyncio
async def test_storage_missing_exact_object_is_unavailable_without_fallback(
    monkeypatch,
):
    key = "tasks/task-1/v3-files/.oddish-manifest.json"
    download = AsyncMock(side_effect=FileNotFoundError(key))
    storage = StorageClient()
    monkeypatch.setattr(storage, "download_json", download)

    result = await storage.read_task_version_manifest(
        task_id="task-1",
        version=3,
        manifest_key=key,
        task_s3_prefix="tasks/task-1/v3/",
        expansion_pending=False,
    )

    assert result == {"status": "unavailable", "files": []}
    download.assert_awaited_once_with(key)


@pytest.mark.asyncio
async def test_storage_rejects_manifest_from_previous_archive_revision(monkeypatch):
    key = "tasks/task-1/v3-files/.oddish-manifest.json"
    download = AsyncMock(
        return_value={
            "task_id": "task-1",
            "version": 3,
            "archive_key": "tasks/task-1/v3/.oddish-task.tar.gz",
            "files": [
                {
                    "path": "task.toml",
                    "size": 12,
                    "sha256": "a" * 64,
                }
            ],
        }
    )
    storage = StorageClient()
    monkeypatch.setattr(storage, "download_json", download)

    result = await storage.read_task_version_manifest(
        task_id="task-1",
        version=3,
        manifest_key=key,
        task_s3_prefix="tasks/task-1/v3-revisions/new-source/",
        expansion_pending=False,
    )

    assert result == {"status": "unavailable", "files": []}
    download.assert_awaited_once_with(key)


@pytest.mark.asyncio
async def test_storage_accepts_matching_legacy_loose_file_source(monkeypatch):
    key = "tasks/task-1/v1-files/.oddish-manifest.json"
    download = AsyncMock(
        return_value={
            "task_id": "task-1",
            "version": 1,
            "source": "loose_files",
            "source_prefix": "tasks/task-1/",
            "files": [
                {
                    "path": "task.toml",
                    "size": 12,
                    "sha256": "b" * 64,
                }
            ],
        }
    )
    storage = StorageClient()
    monkeypatch.setattr(storage, "download_json", download)

    result = await storage.read_task_version_manifest(
        task_id="task-1",
        version=1,
        manifest_key=key,
        task_s3_prefix="tasks/task-1/",
        expansion_pending=False,
    )

    assert result["status"] == "ready"
    assert result["files"] == [
        {
            "path": "task.toml",
            "size": 12,
            "sha256": "b" * 64,
            "skipped": False,
            "skip_reason": None,
        }
    ]


class _Session:
    def __init__(self, *scalar_results):
        self.scalar_results = list(scalar_results)
        self.queries = []

    async def scalar(self, query):
        self.queries.append(query)
        if not self.scalar_results:
            raise AssertionError("unexpected database query")
        return self.scalar_results.pop(0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("manifest_key", "active_job_id", "expected_status"),
    [
        ("tasks/task-1/v3-files/.oddish-manifest.json", None, "ready"),
        (None, "expand-job", "pending"),
        (None, None, "unavailable"),
    ],
)
async def test_manifest_core_enforces_org_and_exact_version_scope(
    monkeypatch,
    manifest_key,
    active_job_id,
    expected_status,
):
    task = SimpleNamespace(id="task-1")
    version_row = SimpleNamespace(
        id="task-1-v3",
        task_id="task-1",
        version=3,
        content_hash="execution-hash",
        expanded_manifest_key=manifest_key,
        task_s3_key="tasks/task-1/v3/",
        task_path="s3://tasks/task-1/v3/",
    )
    session = _Session(
        version_row,
        *([] if manifest_key is not None else [active_job_id]),
    )

    async def authorize(_session, **kwargs):
        assert _session is session
        assert kwargs == {"task_id": "task-1", "org_id": "org-1"}
        return task

    storage = SimpleNamespace()

    async def read_manifest(**kwargs):
        assert kwargs == {
            "task_id": "task-1",
            "version": 3,
            "manifest_key": manifest_key,
            "task_s3_prefix": "tasks/task-1/v3/",
            "expansion_pending": active_job_id is not None,
        }
        return {"status": expected_status, "files": []}

    storage.read_task_version_manifest = AsyncMock(side_effect=read_manifest)
    monkeypatch.setattr(task_detail, "get_task_for_org_core", authorize)
    monkeypatch.setattr(task_detail, "get_storage_client", lambda: storage)

    response = await task_detail.get_task_version_manifest_core(
        session,
        task_id="task-1",
        version=3,
        org_id="org-1",
    )

    assert response.model_dump(mode="json") == {
        "task_id": "task-1",
        "version_id": "task-1-v3",
        "version": 3,
        "content_hash": "execution-hash",
        "status": expected_status,
        "files": [],
    }
    assert "task_versions.task_id" in str(session.queries[0])
    assert "task_versions.version" in str(session.queries[0])
    storage.read_task_version_manifest.assert_awaited_once()


@pytest.mark.asyncio
async def test_manifest_core_rejects_unknown_version_before_storage(monkeypatch):
    session = _Session(None)

    async def authorize(_session, **kwargs):
        assert kwargs == {"task_id": "task-1", "org_id": "org-1"}
        return SimpleNamespace(id="task-1")

    get_storage = AsyncMock()
    monkeypatch.setattr(task_detail, "get_task_for_org_core", authorize)
    monkeypatch.setattr(task_detail, "get_storage_client", get_storage)

    with pytest.raises(HTTPException) as exc:
        await task_detail.get_task_version_manifest_core(
            session,
            task_id="task-1",
            version=99,
            org_id="org-1",
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "Version 99 not found for task task-1"
    get_storage.assert_not_called()


@pytest.mark.asyncio
async def test_manifest_core_preserves_cross_org_404(monkeypatch):
    session = _Session()

    async def reject_cross_org(_session, **kwargs):
        assert kwargs == {"task_id": "task-1", "org_id": "other-org"}
        raise HTTPException(status_code=404, detail="Task task-1 not found")

    get_storage = AsyncMock()
    monkeypatch.setattr(task_detail, "get_task_for_org_core", reject_cross_org)
    monkeypatch.setattr(task_detail, "get_storage_client", get_storage)

    with pytest.raises(HTTPException) as exc:
        await task_detail.get_task_version_manifest_core(
            session,
            task_id="task-1",
            version=3,
            org_id="other-org",
        )

    assert exc.value.status_code == 404
    assert session.queries == []
    get_storage.assert_not_called()

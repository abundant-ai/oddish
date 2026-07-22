"""The task-file JSON content route must round-trip binary bodies (base64 + sha256)."""

from __future__ import annotations

import base64
import hashlib
import io
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.db.storage import (  # noqa: E402
    StorageClient,
    _file_content_fields,
    _read_task_archive_bytes,
)

_GPG_BYTES = b"\x8c\x0d\x04\x03\x03\x02\xab\xcd\x00\xff\xfe"


def _archive_with(path: str, content: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo(path)
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def test_file_content_fields_passes_utf8_text_through():
    fields = _file_content_fields(b"echo hello\n")
    assert fields["content"] == "echo hello\n"
    assert "encoding" not in fields
    assert fields["sha256"] == hashlib.sha256(b"echo hello\n").hexdigest()
    assert fields["size"] == 11


def test_file_content_fields_base64_encodes_binary():
    fields = _file_content_fields(_GPG_BYTES)
    assert fields["encoding"] == "base64"
    assert base64.b64decode(fields["content"]) == _GPG_BYTES
    assert fields["sha256"] == hashlib.sha256(_GPG_BYTES).hexdigest()
    assert fields["size"] == len(_GPG_BYTES)


def test_read_task_archive_bytes_returns_binary_member():
    archive = _archive_with("environment/private_bundle.tar.gz.gpg", _GPG_BYTES)
    raw = _read_task_archive_bytes(archive, "environment/private_bundle.tar.gz.gpg")
    assert raw == _GPG_BYTES


@pytest.mark.asyncio
async def test_get_task_file_content_archive_branch_round_trips_binary(monkeypatch):
    archive = _archive_with("environment/blob.bin", _GPG_BYTES)
    storage = StorageClient()

    async def _resolve_task_prefix(task_id, version):
        return f"tasks/{task_id}/", f"tasks/{task_id}/.oddish-task.tar.gz"

    async def _object_exists(s3_key):
        return s3_key.endswith(".oddish-task.tar.gz")

    async def _load_task_archive(archive_key):
        return archive, []

    async def _head_archive_etag(archive_key):
        return "etag-1"

    monkeypatch.setattr(storage, "_resolve_task_prefix", _resolve_task_prefix)
    monkeypatch.setattr(storage, "object_exists", _object_exists)
    monkeypatch.setattr(storage, "_load_task_archive", _load_task_archive)
    monkeypatch.setattr(storage, "_head_archive_etag", _head_archive_etag)

    result = await storage.get_task_file_content(
        task_id="implement-snowball-abc",
        file_path="environment/blob.bin",
        presign=False,
    )

    assert result["encoding"] == "base64"
    assert base64.b64decode(result["content"]) == _GPG_BYTES
    assert result["sha256"] == hashlib.sha256(_GPG_BYTES).hexdigest()
    assert result["archive_etag"] == "etag-1"

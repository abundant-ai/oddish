from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.db.storage import StorageClient


def _client_with_recorder() -> tuple[StorageClient, list[str]]:
    client = StorageClient()
    uploaded: list[str] = []

    async def _record(file_path, s3_key):
        uploaded.append(s3_key)

    client.upload_file = _record  # type: ignore[assignment]
    return client, uploaded


def test_upload_enforces_authorized_prefix(tmp_path, monkeypatch) -> None:
    (tmp_path / "result.json").write_text("{}", encoding="utf-8")
    client, uploaded = _client_with_recorder()

    prefix = "tasks/task_a/trials/task_a-0/"
    asyncio.run(
        client._upload_directory(tmp_path, prefix, authorized_prefix=prefix)
    )
    # Keys are under the authorized prefix -> uploaded.
    assert uploaded == [f"{prefix}result.json"]


def test_upload_refuses_keys_outside_authorized_prefix(tmp_path) -> None:
    (tmp_path / "result.json").write_text("{}", encoding="utf-8")
    client, uploaded = _client_with_recorder()

    # The write prefix differs from the job's authorized prefix -> refused.
    asyncio.run(
        client._upload_directory(
            tmp_path,
            "tasks/other/trials/other-0/",
            authorized_prefix="tasks/task_a/trials/task_a-0/",
        )
    )
    assert uploaded == []


def test_upload_without_authorized_prefix_is_unrestricted(tmp_path) -> None:
    (tmp_path / "result.json").write_text("{}", encoding="utf-8")
    client, uploaded = _client_with_recorder()

    asyncio.run(
        client._upload_directory(tmp_path, "anything/", authorized_prefix=None)
    )
    assert uploaded == ["anything/result.json"]

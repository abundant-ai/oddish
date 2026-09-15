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
    asyncio.run(client._upload_directory(tmp_path, prefix, authorized_prefix=prefix))
    # Keys are under the authorized prefix -> uploaded.
    assert uploaded == [f"{prefix}result.json"]


def test_upload_refuses_keys_outside_authorized_prefix(tmp_path) -> None:
    import pytest

    (tmp_path / "result.json").write_text("{}", encoding="utf-8")
    client, uploaded = _client_with_recorder()

    # The write prefix differs from the job's authorized prefix -> refused. The
    # upload now fails loudly so a mis-scoped prefix can't read back as a
    # complete upload while silently dropping the file.
    with pytest.raises(RuntimeError, match="authorized prefix"):
        asyncio.run(
            client._upload_directory(
                tmp_path,
                "tasks/other/trials/other-0/",
                authorized_prefix="tasks/task_a/trials/task_a-0/",
            )
        )
    # The out-of-prefix key was never written.
    assert uploaded == []


def test_upload_without_authorized_prefix_is_unrestricted(tmp_path) -> None:
    (tmp_path / "result.json").write_text("{}", encoding="utf-8")
    client, uploaded = _client_with_recorder()

    asyncio.run(client._upload_directory(tmp_path, "anything/", authorized_prefix=None))
    assert uploaded == ["anything/result.json"]


def test_trial_upload_sanitizes_disallowed_key_chars(tmp_path) -> None:
    # Grok's session store nests under a URL-encoded cwd dir; Supabase rejects
    # '%' in object keys, which used to abort the whole trial upload.
    session_dir = tmp_path / "agent" / "grok-session" / "sessions" / "%2Fapp" / "s-1"
    session_dir.mkdir(parents=True)
    (session_dir / "updates.jsonl").write_text("{}\n", encoding="utf-8")
    client, uploaded = _client_with_recorder()

    async def _noop():
        return None

    client._ensure_client = _noop  # type: ignore[assignment]
    prefix = asyncio.run(client.upload_trial_results("task_a-0", tmp_path))

    assert prefix == "tasks/task_a/trials/task_a-0/"
    assert uploaded == [
        f"{prefix}agent/grok-session/sessions/=252Fapp/s-1/updates.jsonl"
    ]


def test_trial_upload_returns_the_exact_nested_attempt_prefix(tmp_path) -> None:
    (tmp_path / "result.json").write_text("{}", encoding="utf-8")
    client, uploaded = _client_with_recorder()

    async def _noop():
        return None

    client._ensure_client = _noop  # type: ignore[assignment]
    root_prefix = "tasks/task_a/trials/task_a-0/"
    prefix = asyncio.run(
        client.upload_trial_results(
            "task_a-0",
            tmp_path,
            authorized_prefix=root_prefix,
            subprefix="analysis-qa/attempt-2",
        )
    )

    assert prefix == f"{root_prefix}analysis-qa/attempt-2/"
    assert uploaded == [f"{prefix}result.json"]


def test_sanitized_trial_keys_stay_within_authorized_prefix(tmp_path) -> None:
    (tmp_path / "%weird").mkdir()
    (tmp_path / "%weird" / "log.txt").write_text("x", encoding="utf-8")
    client, uploaded = _client_with_recorder()

    prefix = "tasks/task_a/trials/task_a-0/"
    asyncio.run(
        client._upload_directory(
            tmp_path, prefix, authorized_prefix=prefix, sanitize_keys=True
        )
    )
    assert uploaded == [f"{prefix}=25weird/log.txt"]


def test_key_sanitization_is_injective() -> None:
    from oddish.db.storage import sanitize_s3_key_chars

    # Names that would collide under naive one-char substitution stay distinct.
    assert sanitize_s3_key_chars("%2Fapp/u.jsonl") == "=252Fapp/u.jsonl"
    assert sanitize_s3_key_chars("=2Fapp/u.jsonl") == "==2Fapp/u.jsonl"
    # Unicode escapes per UTF-8 byte; allowed names pass through untouched.
    assert sanitize_s3_key_chars("café.log") == "caf=C3=A9.log"
    assert sanitize_s3_key_chars("agent/trajectory.json") == "agent/trajectory.json"


def test_task_upload_keeps_exact_names(tmp_path) -> None:
    # Task files must never be renamed: a task references its files by name.
    (tmp_path / "oracle%bin").write_text("x", encoding="utf-8")
    client, uploaded = _client_with_recorder()

    async def _noop():
        return None

    client._ensure_client = _noop  # type: ignore[assignment]
    asyncio.run(client.upload_task_directory("task_a", tmp_path))
    assert uploaded == ["tasks/task_a/oracle%bin"]


def test_trial_upload_publishes_uploaded_inventory_after_bytes_complete(
    tmp_path, monkeypatch
):
    from unittest.mock import AsyncMock
    from oddish.core import file_index

    client, uploaded = _client_with_recorder()
    client._ensure_client = AsyncMock()
    (tmp_path / "result.json").write_text("{}")
    prefix = StorageClient._trial_prefix("task_a-0") + "attempt-2/"
    publications = []

    async def publish(storage, *, trial, files):
        assert storage is client
        assert uploaded == [prefix + "result.json"]
        assert trial.id == "task_a-0"
        assert trial.attempts == 2
        assert trial.trial_s3_key == prefix
        assert [(f["path"], f["size"]) for f in files] == [("result.json", 2)]
        publications.append(trial.id)

    monkeypatch.setattr(file_index, "index_trial_upload", publish)
    assert (
        asyncio.run(
            client.upload_trial_results(
                "task_a-0",
                tmp_path,
                subprefix="attempt-2",
                index_attempt=2,
            )
        )
        == prefix
    )
    assert publications == ["task_a-0"]

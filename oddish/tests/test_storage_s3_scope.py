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


def test_forked_databases_upload_same_trial_without_overwriting(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from oddish.config import settings
    from oddish.core.trial_artifacts import resolve_trial_artifact_layout
    from oddish.workers.queue.job_tokens import s3_write_prefix_for

    objects = {}
    client = StorageClient()

    async def ensure_client():
        pass

    async def upload(file_path, key):
        objects[key] = file_path.read_text()

    async def download(key):
        return objects[key]

    client._ensure_client = ensure_client
    client.upload_file = upload
    client.download_text = download
    prefixes = []
    for namespace, agent in [("main-oddish", "codex"), ("main-oddish-pr-123", "grok")]:
        monkeypatch.setattr(settings, "trial_artifact_namespace", namespace)
        (tmp_path / "result.json").write_text(
            json.dumps({"trial_results": [{"trial_name": agent}]})
        )
        prefix = asyncio.run(
            client.upload_trial_results(
                "task-1568",
                tmp_path,
                authorized_prefix=s3_write_prefix_for("task-1568"),
                subprefix="attempt-1",
            )
        )
        prefixes.append(prefix)
        assert namespace in StorageClient._trial_import_archive_key("task-1568")
    assert prefixes[0] != prefixes[1]
    assert len(objects) == 2
    # Stored pointers remain authoritative even when read from the other deployment.
    for prefix, agent in zip(prefixes, ["codex", "grok"], strict=True):
        layout = asyncio.run(
            resolve_trial_artifact_layout(
                SimpleNamespace(id="task-1568", trial_s3_key=prefix),
                client,
            )
        )
        assert layout.artifact_prefix == f"{prefix}{agent}/"


def test_namespace_preserves_historical_read_fallback(monkeypatch):
    from oddish.config import settings
    from oddish.db.storage import resolve_trial_s3_prefix

    monkeypatch.setattr(settings, "trial_artifact_namespace", "main-oddish-pr-123")
    assert (
        resolve_trial_s3_prefix("task-1568", trial_s3_key=None)
        == "tasks/task/trials/task-1568/"
    )
    assert (
        StorageClient.trial_write_prefix("task-1568")
        == "tasks/task/trials/main-oddish-pr-123/task-1568/"
    )

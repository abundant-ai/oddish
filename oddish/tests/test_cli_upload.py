from __future__ import annotations

import builtins
from pathlib import Path
import sys

import httpx
import pytest
import typer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.cli import api as cli_api


def _write_minimal_task(task_path: Path, *, task_toml: str | None = None) -> None:
    task_path.mkdir()
    (task_path / "task.toml").write_text(
        task_toml
        or """\
version = "1.0"

[metadata]
difficulty = "easy"
description = "first description"

[verifier]
timeout_sec = 120.0

[agent]
timeout_sec = 300.0

[environment]
cpus = 1
memory_mb = 2048
""",
        encoding="utf-8",
    )
    (task_path / "instruction.md").write_text("Solve the task.\n", encoding="utf-8")
    (task_path / "environment").mkdir()
    (task_path / "environment" / "Dockerfile").write_text(
        "FROM alpine:3.20\n", encoding="utf-8"
    )
    (task_path / "tests").mkdir()
    (task_path / "tests" / "test.sh").write_text(
        "#!/bin/sh\nexit 0\n", encoding="utf-8"
    )


class _RetryingUploadClient:
    def __init__(self):
        self.payloads: list[bytes] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def put(
        self,
        _url: str,
        *,
        headers: dict[str, str],
        content: object,
    ) -> httpx.Response:
        assert headers["Content-Length"] == "7"
        assert hasattr(content, "read")
        self.payloads.append(content.read())
        if len(self.payloads) == 1:
            raise httpx.ReadError("transient tls failure")
        return httpx.Response(204)


def test_upload_to_presigned_url_retries_transport_error(
    monkeypatch, tmp_path: Path
) -> None:
    tarball_path = tmp_path / "task.tar.gz"
    tarball_path.write_bytes(b"payload")
    upload_client = _RetryingUploadClient()

    def fake_client(*, timeout: float, follow_redirects: bool):
        assert timeout == 600.0
        assert follow_redirects is True
        return upload_client

    monkeypatch.setattr(cli_api.httpx, "Client", fake_client)
    monkeypatch.setattr(cli_api.time, "sleep", lambda _seconds: None)

    cli_api._upload_to_presigned_url(
        "https://storage.example/upload",
        tarball_path,
        {"Content-Type": "application/gzip"},
    )

    assert upload_client.payloads == [b"payload", b"payload"]


def test_local_task_discovery_fallback_uses_task_model(
    monkeypatch, tmp_path: Path
) -> None:
    (tmp_path / "valid-task").mkdir()
    (tmp_path / "invalid-task").mkdir()

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "harbor.models.job.config":
            raise ImportError("old harbor")
        return real_import(name, *args, **kwargs)

    class FakeTask:
        def __init__(self, path: Path):
            if path.name != "valid-task":
                raise FileNotFoundError(path / "task.toml")

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(cli_api, "Task", FakeTask)

    task_paths = cli_api.get_task_paths_from_local(tmp_path)

    assert task_paths == [tmp_path / "valid-task"]


def test_task_content_hash_ignores_task_metadata_changes(tmp_path: Path) -> None:
    task_path = tmp_path / "task"
    _write_minimal_task(task_path)

    before = cli_api.compute_task_content_hash(task_path)
    (task_path / "task.toml").write_text(
        """\
version = "1.0"

[metadata]
difficulty = "hard"
description = "rewritten grading notes"

[verifier]
timeout_sec = 120.0

[agent]
timeout_sec = 300.0

[environment]
cpus = 1
memory_mb = 2048
""",
        encoding="utf-8",
    )

    assert cli_api.compute_task_content_hash(task_path) == before


def test_task_content_hash_changes_for_runtime_inputs(tmp_path: Path) -> None:
    task_path = tmp_path / "task"
    _write_minimal_task(task_path)

    before = cli_api.compute_task_content_hash(task_path)
    (task_path / "instruction.md").write_text("Solve a different task.\n")
    assert cli_api.compute_task_content_hash(task_path) != before


def test_task_content_hash_changes_for_runtime_task_config(tmp_path: Path) -> None:
    task_path = tmp_path / "task"
    _write_minimal_task(task_path)

    before = cli_api.compute_task_content_hash(task_path)
    (task_path / "task.toml").write_text(
        """\
version = "1.0"

[metadata]
difficulty = "easy"
description = "first description"

[verifier]
timeout_sec = 240.0

[agent]
timeout_sec = 300.0

[environment]
cpus = 1
memory_mb = 2048
""",
        encoding="utf-8",
    )

    assert cli_api.compute_task_content_hash(task_path) != before


def test_task_content_hash_uses_harbor_default_ignores(tmp_path: Path) -> None:
    task_path = tmp_path / "task"
    _write_minimal_task(task_path)

    before = cli_api.compute_task_content_hash(task_path)
    cache_dir = task_path / "environment" / "__pycache__"
    cache_dir.mkdir()
    (cache_dir / "junk.pyc").write_bytes(b"compiled")

    assert cli_api.compute_task_content_hash(task_path) == before


def test_git_lfs_pointer_detection_finds_unresolved_pointer(tmp_path: Path) -> None:
    task_path = tmp_path / "task"
    asset_path = task_path / "environment" / "private_bundle.tar.gz.gpg"
    asset_path.parent.mkdir(parents=True)
    asset_path.write_text(
        "\n".join(
            [
                "version https://git-lfs.github.com/spec/v1",
                "oid sha256:662718d8a1aad9cd2594b91563dbc8384856141c6d4fe4660c8abc7f1c922996",
                "size 1687021",
                "",
            ]
        ),
        encoding="utf-8",
    )

    assert cli_api.find_git_lfs_pointer_files(task_path) == [asset_path]
    with pytest.raises(typer.Exit):
        cli_api.validate_no_git_lfs_pointers(task_path)


def test_git_lfs_pointer_detection_ignores_real_asset(tmp_path: Path) -> None:
    task_path = tmp_path / "task"
    asset_path = task_path / "environment" / "private_bundle.tar.gz.gpg"
    asset_path.parent.mkdir(parents=True)
    asset_path.write_bytes(b"\x8c\r\x04\t\x03\n\xadU\x99\x81\xb7L")

    assert cli_api.find_git_lfs_pointer_files(task_path) == []

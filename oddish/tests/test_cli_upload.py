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


# ---------------------------------------------------------------------------
# Regression: metadata/provenance must reach the row-creating request bodies
# ---------------------------------------------------------------------------
#
# initialize_task_upload creates no DB rows -- only complete_task_upload
# (register=True) and the sweep payload's create_task do. A task_metadata /
# provenance value attached only to the init body is silently dropped for
# every real upload.


class _FakeUploadResponse:
    def __init__(self, status_code: int, json_data: dict | None = None) -> None:
        self.status_code = status_code
        self._json = json_data or {}
        self.text = ""

    def json(self) -> dict:
        return self._json


class _RecordingUploadClient:
    """Fake httpx.Client capturing the JSON body posted to each upload endpoint."""

    def __init__(self) -> None:
        self.bodies: dict[str, dict] = {}

    def __enter__(self) -> "_RecordingUploadClient":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def post(self, url: str, *, json: dict | None = None, **_kwargs: object):
        for suffix in ("/tasks/upload/init", "/tasks/upload/complete"):
            if url.endswith(suffix):
                self.bodies[suffix] = json
                if suffix == "/tasks/upload/init":
                    return _FakeUploadResponse(
                        200,
                        {
                            "task_id": "T1",
                            "name": "task",
                            "version": 1,
                            "upload_url": "https://s3/put",
                            "upload_headers": {},
                        },
                    )
                return _FakeUploadResponse(200, {"task_id": "T1"})
        raise AssertionError(f"unexpected POST {url}")


_TASK_TOML_WITH_BUILD_TIMEOUT = """\
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
build_timeout_sec = 600.0
"""


def test_upload_task_complete_body_carries_metadata_and_provenance(
    monkeypatch, tmp_path: Path
) -> None:
    task_path = tmp_path / "task"
    _write_minimal_task(task_path, task_toml=_TASK_TOML_WITH_BUILD_TIMEOUT)

    monkeypatch.setattr(cli_api, "get_auth_headers", lambda: {})
    monkeypatch.setattr(cli_api, "_upload_to_presigned_url", lambda *_a, **_k: None)
    fake_client = _RecordingUploadClient()
    monkeypatch.setattr(cli_api.httpx, "Client", lambda *_a, **_k: fake_client)

    cli_api.upload_task("http://api", task_path)

    complete_body = fake_client.bodies["/tasks/upload/complete"]
    assert complete_body["task_metadata"]["description"] == "first description"
    assert complete_body["task_metadata"]["cpus"] == 1
    assert "provenance" in complete_body


def test_load_task_metadata_warns_and_drops_oversized_category(
    caplog, tmp_path: Path
) -> None:
    """M-2: a bound violation (category over TaskMetadata's 128-char max)
    must not silently discard the metadata block -- it must warn, naming the
    task, and still return None so the caller's upload proceeds."""
    task_path = tmp_path / "task"
    _write_minimal_task(
        task_path,
        task_toml=f"""\
version = "1.0"

[metadata]
description = "first description"
category = "{"x" * 200}"

[verifier]
timeout_sec = 120.0

[agent]
timeout_sec = 300.0

[environment]
cpus = 1
memory_mb = 2048
""",
    )

    with caplog.at_level("WARNING", logger="oddish.cli.api"):
        result = cli_api.load_task_metadata(task_path)

    assert result is None
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert task_path.name in warnings[0].getMessage()


def test_upload_task_still_succeeds_with_oversized_metadata(
    monkeypatch, tmp_path: Path
) -> None:
    """M-2: the upload itself must still go through when metadata is dropped
    for a bound violation -- only the metadata block is omitted, not the
    whole upload."""
    task_path = tmp_path / "task"
    _write_minimal_task(
        task_path,
        task_toml=f"""\
version = "1.0"

[metadata]
description = "first description"
category = "{"x" * 200}"

[verifier]
timeout_sec = 120.0

[agent]
timeout_sec = 300.0

[environment]
cpus = 1
memory_mb = 2048
build_timeout_sec = 600.0
""",
    )

    monkeypatch.setattr(cli_api, "get_auth_headers", lambda: {})
    monkeypatch.setattr(cli_api, "_upload_to_presigned_url", lambda *_a, **_k: None)
    fake_client = _RecordingUploadClient()
    monkeypatch.setattr(cli_api.httpx, "Client", lambda *_a, **_k: fake_client)

    result = cli_api.upload_task("http://api", task_path)

    assert result == {"task_id": "T1"}
    complete_body = fake_client.bodies["/tasks/upload/complete"]
    assert "task_metadata" not in complete_body


def test_build_sweep_payload_carries_metadata_and_provenance(tmp_path: Path) -> None:
    task_path = tmp_path / "task"
    _write_minimal_task(task_path)

    task_metadata = cli_api.load_task_metadata(task_path)
    provenance = cli_api.detect_provenance(task_path, env={})

    payload = cli_api.build_sweep_payload(
        task_id="T1",
        configs=[{"agent": "claude-code", "model": "m", "n_trials": 1}],
        environment=None,
        user=None,
        priority="low",
        experiment_id=None,
        task_metadata=task_metadata,
        provenance=provenance,
    )

    assert payload["task_metadata"]["description"] == "first description"
    assert payload["task_metadata"]["cpus"] == 1
    assert "provenance" in payload

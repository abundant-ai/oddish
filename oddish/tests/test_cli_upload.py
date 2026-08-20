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


_TPU_TASK_WITHOUT_TOPOLOGY = """\
version = "1.0"

[metadata]
difficulty = "easy"
description = "tpu task"

[environment]
cpus = 1
memory_mb = 2048

[environment.tpu]
type = "v6e"
"""


class TestRejectedConfigIsNotReportedAsMissingFiles:
    """A task whose config is rejected must not be called a missing-files error.

    `topology` is required on a TPU spec. Removing it makes `task.toml`
    genuinely invalid, which is correct. The reported problem was the message:
    the CLI answered "A task directory must contain: task.toml, instruction.md,
    environment/, tests/" while all four were present, so the reader looked for
    files instead of the one missing key.
    """

    def test_the_directory_is_still_recognised_as_a_task(self, tmp_path: Path) -> None:
        task_path = tmp_path / "tpu-task"
        _write_minimal_task(task_path, task_toml=_TPU_TASK_WITHOUT_TOPOLOGY)

        assert cli_api.holds_task_config(task_path) is True
        assert cli_api.is_task_dir(task_path) is False

    def test_the_reason_names_the_field(self, tmp_path: Path) -> None:
        task_path = tmp_path / "tpu-task"
        _write_minimal_task(task_path, task_toml=_TPU_TASK_WITHOUT_TOPOLOGY)

        error = cli_api.task_load_error(task_path)
        assert error is not None
        assert "topology" in str(error)

    def test_a_valid_task_reports_no_error(self, tmp_path: Path) -> None:
        task_path = tmp_path / "ok-task"
        _write_minimal_task(task_path)

        assert cli_api.task_load_error(task_path) is None
        assert cli_api.is_task_dir(task_path) is True

    def test_a_directory_without_a_task_toml_is_not_a_task(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "not-a-task").mkdir()
        assert cli_api.holds_task_config(tmp_path / "not-a-task") is False

    def test_a_step_task_is_recognised_without_a_top_level_instruction(
        self, tmp_path: Path
    ) -> None:
        """Harbor requires instruction.md only when a task has no steps, and
        requires tests/ only when no verifier environment is declared. A fixed
        list of required entries would call these shapes "not a task" and hand
        back the wrong message."""
        task_path = tmp_path / "step-task"
        (task_path / "environment").mkdir(parents=True)
        (task_path / "environment" / "Dockerfile").write_text("FROM alpine:3.20\n")
        (task_path / "steps" / "one").mkdir(parents=True)
        (task_path / "steps" / "one" / "instruction.md").write_text("step\n")
        (task_path / "task.toml").write_text(
            "[environment]\ncpus = 1\n\n"
            '[environment.tpu]\ntype = "v6e"\n\n'
            '[[steps]]\nname = "one"\n'
        )

        assert cli_api.holds_task_config(task_path) is True
        error = cli_api.task_load_error(task_path)
        assert error is not None and "topology" in str(error)

    def test_cli_reports_the_config_error_not_the_file_list(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The end-to-end message a user sees."""
        task_path = tmp_path / "tpu-task"
        _write_minimal_task(task_path, task_toml=_TPU_TASK_WITHOUT_TOPOLOGY)

        with pytest.raises(typer.Exit):
            cli_api.resolve_local_task_paths(
                path=Path(task_path),
                path_option=None,
                dataset=None,
                task_names=None,
                exclude_task_names=None,
                n_tasks=None,
                quiet=True,
            )

        printed = capsys.readouterr()
        combined = printed.out + printed.err
        assert "topology" in combined, combined
        assert "must contain" not in combined, (
            "the files are all present; naming them sends the reader to the wrong place"
        )


class TestInvalidTaskInADatasetIsNotDroppedSilently:
    """Discovery removes an invalid task before validate_tasks() sees it.

    Uploading a dataset of N tasks where one is invalid used to upload N-1 and
    print nothing, so the author believed all N went up. That is quieter than
    the reported bug, and worse.
    """

    def test_the_skipped_task_is_named_with_its_reason(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_minimal_task(tmp_path / "good")
        _write_minimal_task(tmp_path / "bad", task_toml=_TPU_TASK_WITHOUT_TOPOLOGY)

        found = cli_api.get_task_paths_from_local(dataset_path=tmp_path)

        assert sorted(p.name for p in found) == ["good"]
        printed = capsys.readouterr()
        combined = printed.out + printed.err
        assert "bad" in combined
        assert "topology" in combined, combined

    def test_a_non_task_directory_stays_quiet(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Only children that carry a task.toml are reported."""
        _write_minimal_task(tmp_path / "good")
        (tmp_path / "notes").mkdir()
        (tmp_path / "notes" / "README.md").write_text("nothing here\n")

        cli_api.get_task_paths_from_local(dataset_path=tmp_path)

        printed = capsys.readouterr()
        assert "notes" not in (printed.out + printed.err)

    def test_a_task_removed_by_a_filter_is_not_called_an_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--task-name and -n drop valid tasks; those are not failures."""
        _write_minimal_task(tmp_path / "keep")
        _write_minimal_task(tmp_path / "drop")

        found = cli_api.get_task_paths_from_local(
            dataset_path=tmp_path, task_names=["keep"]
        )

        assert sorted(p.name for p in found) == ["keep"]
        printed = capsys.readouterr()
        assert "drop" not in (printed.out + printed.err)

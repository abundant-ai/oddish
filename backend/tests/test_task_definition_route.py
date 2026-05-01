"""Tests for the GET /tasks/{task_id}/definition endpoint.

These tests cover two layers:

1. Pure unit tests for ``build_task_archive`` — the service function that does
   all the real work. No DB, auth, or HTTP machinery involved.

2. A smoke-test confirming the return-type contract that the route handler
   depends on from ``build_task_archive``.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from api.services.task_archive import TaskArchive, build_task_archive


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tar_members(body: bytes) -> list[str]:
    """Return sorted list of member names from a gzipped tar."""
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
        return sorted(m.name for m in tar.getmembers())


def _tar_read(body: bytes, member: str) -> str:
    """Read a single text member from a gzipped tar."""
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
        f = tar.extractfile(member)
        assert f is not None, f"member {member!r} not found"
        return f.read().decode()


# ---------------------------------------------------------------------------
# Unit tests for build_task_archive
# ---------------------------------------------------------------------------


def test_returns_tar_with_priority_files(tmp_path):
    task_dir = tmp_path / "test-task"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text(
        "name = 'my-task'\nverifier_command = 'bash verifier/run.sh'\n"
    )
    (task_dir / "instruction.md").write_text("# do the thing\n")
    (task_dir / "verifier").mkdir()
    (task_dir / "verifier" / "run.sh").write_text("#!/bin/sh\necho 1\n")

    archive = build_task_archive(task_dir, fallback_name="fallback")

    assert isinstance(archive, TaskArchive)
    members = _tar_members(archive.body)
    assert "task.toml" in members
    assert "instruction.md" in members
    assert "verifier/run.sh" in members


def test_task_name_and_verifier_command_extracted_from_toml(tmp_path):
    task_dir = tmp_path / "test-task"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text(
        "name = 'extracted-name'\nverifier_command = 'python verifier/check.py'\n"
    )

    archive = build_task_archive(task_dir, fallback_name="fallback")

    assert archive.task_name == "extracted-name"
    assert archive.verifier_command == "python verifier/check.py"


def test_falls_back_to_fallback_name_when_toml_missing(tmp_path):
    task_dir = tmp_path / "test-task"
    task_dir.mkdir()
    (task_dir / "instruction.md").write_text("# no toml here\n")

    archive = build_task_archive(task_dir, fallback_name="my-fallback")

    assert archive.task_name == "my-fallback"
    assert archive.verifier_command == "bash verifier/run.sh"


def test_falls_back_to_fallback_name_when_toml_has_no_name(tmp_path):
    task_dir = tmp_path / "test-task"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text("timeout_sec = 60\n")

    archive = build_task_archive(task_dir, fallback_name="my-fallback")

    assert archive.task_name == "my-fallback"


def test_skips_dotfiles_and_skip_dirs(tmp_path):
    task_dir = tmp_path / "test-task"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text("name = 'test'\n")
    (task_dir / ".env").write_text("SECRET=oops\n")
    git_dir = task_dir / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\n")
    pycache = task_dir / "__pycache__"
    pycache.mkdir()
    (pycache / "foo.pyc").write_bytes(b"\x00" * 10)

    archive = build_task_archive(task_dir, fallback_name="test")

    members = _tar_members(archive.body)
    assert ".env" not in members
    assert not any(".git" in m for m in members)
    assert not any("__pycache__" in m for m in members)


def test_discretionary_tail_included(tmp_path):
    task_dir = tmp_path / "test-task"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text("name = 'test'\n")
    (task_dir / "extra.txt").write_text("some extra content\n")

    archive = build_task_archive(task_dir, fallback_name="test")

    members = _tar_members(archive.body)
    assert "extra.txt" in members


def test_file_content_preserved(tmp_path):
    task_dir = tmp_path / "test-task"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text("name = 'test'\n")
    (task_dir / "instruction.md").write_text("# Hello world\n")

    archive = build_task_archive(task_dir, fallback_name="test")

    content = _tar_read(archive.body, "instruction.md")
    assert content == "# Hello world\n"


def test_oversize_file_excluded_from_discretionary_tail(tmp_path):
    task_dir = tmp_path / "test-task"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text("name = 'test'\n")
    # Write a file just over the 1MB per-file cap.
    big_file = task_dir / "big.bin"
    big_file.write_bytes(b"x" * (1 * 1024 * 1024 + 1))

    archive = build_task_archive(task_dir, fallback_name="test")

    members = _tar_members(archive.body)
    assert "big.bin" not in members


def test_body_is_valid_gzip(tmp_path):
    task_dir = tmp_path / "test-task"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text("name = 'test'\n")

    archive = build_task_archive(task_dir, fallback_name="test")

    # gzip magic bytes
    assert archive.body[:2] == b"\x1f\x8b"


def test_verifier_subdir_included_recursively(tmp_path):
    task_dir = tmp_path / "test-task"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text("name = 'test'\n")
    verifier = task_dir / "verifier"
    verifier.mkdir()
    (verifier / "run.sh").write_text("#!/bin/sh\necho done\n")
    nested = verifier / "lib"
    nested.mkdir()
    (nested / "helper.py").write_text("def check(): pass\n")

    archive = build_task_archive(task_dir, fallback_name="test")

    members = _tar_members(archive.body)
    assert "verifier/run.sh" in members
    assert "verifier/lib/helper.py" in members


# ---------------------------------------------------------------------------
# Return-type contract test (guards the route handler dependency)
# ---------------------------------------------------------------------------


def test_build_task_archive_returns_task_archive_dataclass(tmp_path):
    """Confirm the return type contract the route handler depends on."""
    task_dir = tmp_path / "test-task"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text(
        "name = 'contract-test'\nverifier_command = 'bash verifier/run.sh'\n"
    )

    result = build_task_archive(task_dir, fallback_name="fallback")

    assert hasattr(result, "body")
    assert hasattr(result, "task_name")
    assert hasattr(result, "verifier_command")
    assert isinstance(result.body, bytes)
    assert isinstance(result.task_name, str)
    assert isinstance(result.verifier_command, str)

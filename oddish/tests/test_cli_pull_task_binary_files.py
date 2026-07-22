"""Tests that ``oddish pull --include-task-files`` preserves binary task files.

Tasks carry binary members (oracle ELF binaries, GPG-encrypted private bundles).
Pull used to decode every task file as UTF-8, so those members raised
UnicodeDecodeError, got recorded as ``errors/<path>.error.txt`` and were dropped
-- leaving a tree that looks complete but cannot be re-uploaded.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import importlib  # noqa: E402
from unittest.mock import patch  # noqa: E402

# ``oddish.cli`` re-exports the ``pull`` *command function*, shadowing the
# submodule, so import the module explicitly.
pull_cli = importlib.import_module("oddish.cli.pull")

# First bytes of a real GPG-encrypted private bundle: 0x8c is not valid UTF-8
# and is what made the old decode path raise.
_GPG_BYTES = b"\x8c\x0d\x04\x03\x03\x02\xab\xcd\x00\xff\xfe"
# ELF magic, as in a task's oracle binary.
_ELF_BYTES = b"\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00"


def test_download_task_file_returns_binary_unchanged_from_presigned_url():
    with patch.object(
        pull_cli, "_download_presigned_bytes", return_value=(_GPG_BYTES, None)
    ):
        content, err = pull_cli._download_task_file(
            client=None,
            task_id="implement-snowball-abc",
            remote_path="environment/private_bundle.tar.gz.gpg",
            download_url="https://s3.example/presigned",
        )

    assert err is None
    assert content == _GPG_BYTES


def test_download_and_save_task_file_writes_binary_byte_exact(tmp_path):
    """The whole save path must round-trip bytes, not write an .error.txt."""
    local_file = tmp_path / "files" / "environment" / "binary" / "snowball"
    error_dir = tmp_path / "errors"

    with patch.object(
        pull_cli, "_download_presigned_bytes", return_value=(_ELF_BYTES, None)
    ):
        status = pull_cli._download_and_save_task_file(
            client=None,
            task_id="implement-snowball-abc",
            remote_path="environment/binary/snowball",
            download_url="https://s3.example/presigned",
            local_file=local_file,
            error_dir=error_dir,
            rel=Path("environment/binary/snowball"),
        )

    assert status == "saved"
    assert local_file.read_bytes() == _ELF_BYTES
    # The old behavior left the file absent and an error stub in its place.
    assert not error_dir.exists()


def test_download_and_save_task_file_still_reports_real_errors(tmp_path):
    local_file = tmp_path / "files" / "instruction.md"
    error_dir = tmp_path / "errors"

    with patch.object(
        pull_cli, "_download_presigned_bytes", return_value=(None, "403: denied")
    ):
        status = pull_cli._download_and_save_task_file(
            client=None,
            task_id="implement-snowball-abc",
            remote_path="instruction.md",
            download_url="https://s3.example/presigned",
            local_file=local_file,
            error_dir=error_dir,
            rel=Path("instruction.md"),
        )

    assert status == "error"
    assert not local_file.exists()
    assert (error_dir / "instruction.md.error.txt").read_text() == "403: denied"


def test_download_task_file_json_fallback_returns_encoded_text():
    """The non-presigned JSON route carries text only; it must still yield bytes."""

    class _FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"content": "compile.sh contents\n"}

    class _FakeClient:
        @staticmethod
        def get(url, params=None):
            return _FakeResponse()

    content, err = pull_cli._download_task_file(
        client=_FakeClient(),
        task_id="implement-snowball-abc",
        remote_path="compile.sh",
        download_url=None,
    )

    assert err is None
    assert content == b"compile.sh contents\n"


def test_download_task_file_json_fallback_decodes_base64_binary():
    """The server base64-encodes non-UTF-8 bodies; the client must restore bytes."""
    import base64

    class _FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "content": base64.b64encode(_GPG_BYTES).decode("ascii"),
                "encoding": "base64",
            }

    class _FakeClient:
        @staticmethod
        def get(url, params=None):
            return _FakeResponse()

    content, err = pull_cli._download_task_file(
        client=_FakeClient(),
        task_id="implement-snowball-abc",
        remote_path="environment/private_bundle.tar.gz.gpg",
        download_url=None,
    )

    assert err is None
    assert content == _GPG_BYTES


def test_download_and_save_task_file_rejects_size_mismatch(tmp_path):
    """A truncated download must be recorded as an error, not saved as truth."""
    local_file = tmp_path / "files" / "environment" / "binary" / "snowball"
    error_dir = tmp_path / "errors"
    checksums: dict[str, str] = {}

    with patch.object(
        pull_cli, "_download_presigned_bytes", return_value=(_ELF_BYTES, None)
    ):
        status = pull_cli._download_and_save_task_file(
            client=None,
            task_id="implement-snowball-abc",
            remote_path="environment/binary/snowball",
            download_url="https://s3.example/presigned",
            local_file=local_file,
            error_dir=error_dir,
            rel=Path("environment/binary/snowball"),
            expected_size=len(_ELF_BYTES) + 7,
            checksums=checksums,
            checksum_key="tasks/implement-snowball-abc/files/environment/binary/snowball",
        )

    assert status == "error"
    assert not local_file.exists()
    assert checksums == {}
    error_text = (error_dir / "environment/binary/snowball.error.txt").read_text()
    assert "size mismatch" in error_text


def test_download_and_save_task_file_records_sha256(tmp_path):
    import hashlib

    local_file = tmp_path / "files" / "environment" / "binary" / "snowball"
    checksums: dict[str, str] = {}
    key = "tasks/implement-snowball-abc/files/environment/binary/snowball"

    with patch.object(
        pull_cli, "_download_presigned_bytes", return_value=(_ELF_BYTES, None)
    ):
        status = pull_cli._download_and_save_task_file(
            client=None,
            task_id="implement-snowball-abc",
            remote_path="environment/binary/snowball",
            download_url="https://s3.example/presigned",
            local_file=local_file,
            error_dir=tmp_path / "errors",
            rel=Path("environment/binary/snowball"),
            expected_size=len(_ELF_BYTES),
            checksums=checksums,
            checksum_key=key,
        )

    assert status == "saved"
    assert checksums == {key: hashlib.sha256(_ELF_BYTES).hexdigest()}


def test_extract_task_archive_round_trips_binary_and_checksums(tmp_path):
    """The archive path must extract binary members byte-exact with checksums."""
    import hashlib
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("environment/private_bundle.tar.gz.gpg")
        info.size = len(_GPG_BYTES)
        tar.addfile(info, io.BytesIO(_GPG_BYTES))

    checksums: dict[str, str] = {}
    summary = pull_cli._extract_task_archive(
        buffer.getvalue(),
        tmp_path / "files",
        {"task_files_saved": 0, "task_files_skipped": 0, "task_file_errors": 0},
        checksums,
        "tasks/implement-snowball-abc/files/",
    )

    saved = tmp_path / "files" / "environment" / "private_bundle.tar.gz.gpg"
    assert summary["task_files_saved"] == 1
    assert saved.read_bytes() == _GPG_BYTES
    assert checksums == {
        "tasks/implement-snowball-abc/files/environment/private_bundle.tar.gz.gpg": (
            hashlib.sha256(_GPG_BYTES).hexdigest()
        )
    }

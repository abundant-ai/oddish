"""CLI-side hashing and manifest HTTP contract tests."""

from __future__ import annotations

from unittest.mock import patch

import httpx

from oddish.cli import api


def _write_minimal_task(task_dir) -> None:
    (task_dir / "task.toml").write_text(
        "[metadata]\n"
        'name = "manifest-test"\n'
        "\n"
        "[environment]\n"
        "build_timeout_sec = 1800.0\n"
        "\n"
        "[agent]\n"
        "timeout_sec = 18000.0\n"
        "\n"
        "[verifier]\n"
        "timeout_sec = 1500.0\n",
        encoding="utf-8",
    )


def test_hash_local_task_files_distinguishes_all_change_kinds(tmp_path):
    _write_minimal_task(tmp_path)
    (tmp_path / "unchanged.txt").write_bytes(b"same")
    (tmp_path / "modified.txt").write_bytes(b"before")
    (tmp_path / "deleted.txt").write_bytes(b"gone")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "kept.bin").write_bytes(b"\x00\xff")

    before = api.hash_local_task_files(tmp_path)
    (tmp_path / "modified.txt").write_bytes(b"after")
    (tmp_path / "deleted.txt").unlink()
    (tmp_path / "added.txt").write_bytes(b"new")
    after = api.hash_local_task_files(tmp_path)

    assert after["unchanged.txt"] == before["unchanged.txt"]
    assert after["nested/kept.bin"] == before["nested/kept.bin"]
    assert after["modified.txt"].sha256 != before["modified.txt"].sha256
    assert "added.txt" not in before and "added.txt" in after
    assert "deleted.txt" in before and "deleted.txt" not in after
    assert after["added.txt"].size == 3
    assert all(entry.path == path for path, entry in after.items())


def test_local_hash_skips_symlinks_like_archive_expansion(tmp_path):
    _write_minimal_task(tmp_path)
    target = tmp_path / "real.txt"
    target.write_bytes(b"real")
    (tmp_path / "linked.txt").symlink_to(target)

    manifest = api.hash_local_task_files(tmp_path)

    assert "real.txt" in manifest
    assert "linked.txt" not in manifest


def test_raw_archive_file_can_change_without_execution_content_hash(tmp_path):
    _write_minimal_task(tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text("before\n", encoding="utf-8")

    execution_before = api.compute_task_content_hash(tmp_path)
    files_before = api.hash_local_task_files(tmp_path)
    readme.write_text("after\n", encoding="utf-8")
    execution_after = api.compute_task_content_hash(tmp_path)
    files_after = api.hash_local_task_files(tmp_path)

    assert execution_after == execution_before
    assert files_after["README.md"].sha256 != files_before["README.md"].sha256


def test_get_task_version_manifest_uses_direct_exact_route():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "task_id": "task with spaces",
                "version_id": "task with spaces-v7",
                "version": 7,
                "content_hash": "execution-hash",
                "status": "ready",
                "files": [
                    {
                        "path": "task.toml",
                        "size": 12,
                        "sha256": "a" * 64,
                        "skipped": False,
                        "skip_reason": None,
                    }
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    with (
        patch("oddish.cli.api.httpx.Client", side_effect=client_factory),
        patch("oddish.cli.api.get_auth_headers", return_value={}),
    ):
        response = api.get_task_version_manifest(
            "https://example.test/", "task with spaces", 7
        )

    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/tasks/task with spaces/versions/7/manifest"
    assert response.status == "ready"
    assert response.files[0].sha256 == "a" * 64

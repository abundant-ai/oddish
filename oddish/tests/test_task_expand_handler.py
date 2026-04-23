"""Tests for ``oddish.workers.queue.task_expand_handler.run_task_expand_job``.

The handler is tightly coupled to S3 + the DB: we stub both behind a
small fake that captures ``upload_bytes`` calls so we can assert on the
manifest shape, short-circuit path, and size-skip behavior without
needing live infrastructure.
"""

from __future__ import annotations

import io
import json
import sys
import tarfile
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import settings  # noqa: E402
from oddish.db.storage import StorageClient  # noqa: E402
from oddish.workers.queue import task_expand_handler  # noqa: E402


def _make_archive(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path, data in files.items():
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class _FakeS3:
    """Just enough of the aioboto3 client surface for
    ``run_task_expand_job`` to head/upload/download objects.

    Shares the backing ``objects`` / ``etags`` dicts with the parent
    ``_FakeStorage`` so a manifest written via ``upload_bytes`` is
    visible to a later ``head_object`` (and vice versa) without
    test-setup juggling.
    """

    def __init__(
        self,
        objects: dict[str, bytes],
        etags: dict[str, str],
    ) -> None:
        self._objects = objects
        self._etags = etags

    async def head_object(self, Bucket: str, Key: str) -> dict:
        if Key not in self._objects:
            raise RuntimeError(f"NoSuchKey: {Key}")
        return {
            "ContentLength": len(self._objects[Key]),
            "ETag": self._etags.get(Key, '"etag-default"'),
        }

    async def put_object(self, Bucket: str, Key: str, Body: bytes, **kwargs) -> dict:
        self._objects[Key] = (
            Body if isinstance(Body, (bytes, bytearray)) else bytes(Body)
        )
        return {}


class _FakeStorage:
    """Stand-in for ``StorageClient`` that ``run_task_expand_job`` uses.

    Accepts either an ``archive_key`` (tarball layout) or ``loose_objects``
    (pre-archive loose-file layout), or both. Tests can omit ``archive_key``
    to simulate a task that has no archive anywhere.
    """

    _EXPANDED_MANIFEST_OBJECT_NAME = StorageClient._EXPANDED_MANIFEST_OBJECT_NAME

    def __init__(
        self,
        *,
        archive_key: str | None = None,
        archive_bytes: bytes | None = None,
        etag: str = '"etag-v1"',
        manifest_bytes: bytes | None = None,
        loose_objects: dict[str, bytes] | None = None,
    ) -> None:
        self._etag = etag
        self._objects: dict[str, bytes] = {}
        self._etags: dict[str, str] = {}
        if archive_key is not None and archive_bytes is not None:
            self._objects[archive_key] = archive_bytes
            self._etags[archive_key] = etag
        if loose_objects:
            for k, v in loose_objects.items():
                self._objects[k] = v
        if manifest_bytes is not None and archive_key is not None:
            archive_dir = archive_key.rsplit("/", 1)[0]  # tasks/{id}/v{N}
            # Derive the ``-files/`` sibling prefix from the versioned
            # archive key: ``tasks/{id}/v{N}/.oddish-task.tar.gz`` ->
            # ``tasks/{id}/v{N}-files/.oddish-manifest.json``.
            manifest_key = f"{archive_dir}-files/.oddish-manifest.json"
            self._objects[manifest_key] = manifest_bytes
        self._s3 = _FakeS3(self._objects, self._etags)
        self.upload_calls: list[tuple[str, bytes, str | None]] = []

    async def _ensure_client(self) -> None:
        return None

    async def object_exists(self, s3_key: str) -> bool:
        return s3_key in self._objects

    async def download_bytes(self, s3_key: str) -> bytes:
        return self._objects[s3_key]

    async def download_json(self, s3_key: str) -> dict:
        return json.loads(self._objects[s3_key].decode("utf-8"))

    # Tests can set ``reject_key_substrings`` to simulate Supabase's S3
    # proxy rejecting certain key characters (``[``, URL-encoded bytes,
    # etc.) without monkey-patching the entire upload path.
    reject_key_substrings: tuple[str, ...] = ()

    async def upload_bytes(
        self,
        data: bytes,
        s3_key: str,
        *,
        content_type: str | None = None,
    ) -> None:
        for needle in self.reject_key_substrings:
            if needle in s3_key:
                raise RuntimeError(
                    f"ClientError: An error occurred (InvalidKey) when "
                    f"calling the PutObject operation: Invalid key: {s3_key}"
                )
        self._objects[s3_key] = data
        self.upload_calls.append((s3_key, data, content_type))

    async def _load_task_archive(self, archive_key: str):
        return (self._objects[archive_key], [])

    async def list_objects_all(self, prefix: str) -> list[dict]:
        return [
            {"key": k, "size": len(v), "last_modified": None}
            for k, v in self._objects.items()
            if k.startswith(prefix)
        ]

    async def _resolve_task_prefix(
        self, task_id: str, version: int | None
    ) -> tuple[str, str]:
        """Mirror ``StorageClient._resolve_task_prefix`` for the fake.

        Prefers the versioned archive key, falls back to the unversioned
        path when the versioned object doesn't exist. Tests exercising
        legacy v1 layouts can stage the archive at either location and
        the handler will still resolve it.
        """
        if version is not None:
            vroot = f"tasks/{task_id}/v{version}/"
            varchive = f"{vroot}{StorageClient._TASK_ARCHIVE_OBJECT_NAME}"
            if varchive in self._objects:
                return vroot, varchive
            fallback_archive = (
                f"tasks/{task_id}/{StorageClient._TASK_ARCHIVE_OBJECT_NAME}"
            )
            if fallback_archive in self._objects:
                return f"tasks/{task_id}/", fallback_archive
            return vroot, varchive
        return (
            f"tasks/{task_id}/",
            f"tasks/{task_id}/{StorageClient._TASK_ARCHIVE_OBJECT_NAME}",
        )


@asynccontextmanager
async def _null_session():
    class _S:
        async def get(self, *_args, **_kwargs):
            return None

        async def commit(self):
            return None

        async def scalar(self, *_args, **_kwargs):
            return None

    yield _S()


@pytest.fixture
def _patched_get_session(monkeypatch):
    monkeypatch.setattr(task_expand_handler, "get_session", lambda: _null_session())


# ---------------------------------------------------------------------------
# Core behaviors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expand_writes_members_and_manifest(monkeypatch, _patched_get_session):
    archive_bytes = _make_archive(
        {
            "task.toml": b"name = 'demo'\n",
            "verifier/check.py": b"print(1)\n",
        }
    )
    storage = _FakeStorage(
        archive_key="tasks/task-abc/v1/.oddish-task.tar.gz",
        archive_bytes=archive_bytes,
    )
    monkeypatch.setattr(task_expand_handler, "get_storage_client", lambda: storage)

    summary = await task_expand_handler.run_task_expand_job(
        task_id="task-abc", version=1
    )

    assert summary["status"] == "expanded"
    assert summary["files"] == 2

    expanded_prefix = "tasks/task-abc/v1-files/"
    assert f"{expanded_prefix}task.toml" in storage._objects
    assert f"{expanded_prefix}verifier/check.py" in storage._objects
    assert (
        f"{expanded_prefix}{StorageClient._EXPANDED_MANIFEST_OBJECT_NAME}"
        in storage._objects
    )

    manifest = json.loads(
        storage._objects[
            f"{expanded_prefix}{StorageClient._EXPANDED_MANIFEST_OBJECT_NAME}"
        ].decode("utf-8")
    )
    assert manifest["task_id"] == "task-abc"
    assert manifest["version"] == 1
    assert manifest["archive_etag"] == '"etag-v1"'
    assert {f["path"] for f in manifest["files"]} == {
        "task.toml",
        "verifier/check.py",
    }


@pytest.mark.asyncio
async def test_expand_falls_back_to_unversioned_archive(
    monkeypatch, _patched_get_session
):
    """Legacy v1 tasks have archives at ``tasks/{id}/.oddish-task.tar.gz``
    (no ``v{N}/`` sub-prefix, from before task versioning). The handler
    must resolve that fallback the same way ``list_task_files`` /
    ``get_task_file_content`` do, otherwise every legacy version 404s
    and the backfill retries 6 times before permanently failing."""
    archive_bytes = _make_archive({"task.toml": b"name = 'legacy'\n"})
    storage = _FakeStorage(
        # Archive lives at the unversioned path; the versioned key is absent.
        archive_key="tasks/task-legacy/.oddish-task.tar.gz",
        archive_bytes=archive_bytes,
    )
    monkeypatch.setattr(task_expand_handler, "get_storage_client", lambda: storage)

    summary = await task_expand_handler.run_task_expand_job(
        task_id="task-legacy", version=1
    )

    assert summary["status"] == "expanded"
    # Expanded objects still land under the versioned sibling prefix so
    # readers can locate them with the ``version`` threaded through from
    # the router.
    expanded_prefix = "tasks/task-legacy/v1-files/"
    assert f"{expanded_prefix}task.toml" in storage._objects
    assert (
        f"{expanded_prefix}{StorageClient._EXPANDED_MANIFEST_OBJECT_NAME}"
        in storage._objects
    )

    manifest = json.loads(
        storage._objects[
            f"{expanded_prefix}{StorageClient._EXPANDED_MANIFEST_OBJECT_NAME}"
        ].decode("utf-8")
    )
    # The manifest records the actual resolved archive key so operators
    # can tell which source produced the expansion.
    assert manifest["archive_key"] == "tasks/task-legacy/.oddish-task.tar.gz"


@pytest.mark.asyncio
async def test_expand_migrates_loose_file_task_without_archive(
    monkeypatch, _patched_get_session
):
    """Pre-archive uploads (``upload_task_directory``) stored each task
    file as its own S3 object under ``tasks/{id}/`` with no tarball
    anywhere. The expansion handler must migrate those directly into
    the canonical ``v{N}-files/`` layout so the reader has one code
    path for every task, regardless of how it was uploaded.

    Trial artifacts under ``trials/`` and other versioned sub-prefixes
    must be excluded — only the task's own source files get migrated.
    """
    storage = _FakeStorage(
        # No archive anywhere: archive_key is None.
        loose_objects={
            "tasks/task-loose/task.toml": b"name = 'loose'\n",
            "tasks/task-loose/instruction.md": b"do the thing\n",
            "tasks/task-loose/tests/test.sh": b"#!/bin/sh\n",
            # Trial artifacts live under ``trials/`` and must NOT be
            # migrated into the task's expanded file list.
            "tasks/task-loose/trials/task-loose-0/result.json": b"{}",
            "tasks/task-loose/trials/task-loose-0/job.log": b"...",
            # Any versioned sibling layout (e.g. partially-migrated
            # state) should also be ignored.
            "tasks/task-loose/v2/.oddish-task.tar.gz": b"\x00",
            "tasks/task-loose/v1-files/stale.txt": b"stale",
        },
    )
    monkeypatch.setattr(task_expand_handler, "get_storage_client", lambda: storage)

    summary = await task_expand_handler.run_task_expand_job(
        task_id="task-loose", version=1
    )

    assert summary["status"] == "expanded"
    assert summary["source"] == "loose_files"
    assert summary["files"] == 3

    expanded_prefix = "tasks/task-loose/v1-files/"
    assert storage._objects[f"{expanded_prefix}task.toml"] == b"name = 'loose'\n"
    assert storage._objects[f"{expanded_prefix}instruction.md"] == b"do the thing\n"
    assert storage._objects[f"{expanded_prefix}tests/test.sh"] == b"#!/bin/sh\n"
    assert f"{expanded_prefix}trials/task-loose-0/result.json" not in storage._objects
    assert f"{expanded_prefix}v2/.oddish-task.tar.gz" not in storage._objects

    manifest = json.loads(
        storage._objects[
            f"{expanded_prefix}{StorageClient._EXPANDED_MANIFEST_OBJECT_NAME}"
        ].decode("utf-8")
    )
    assert manifest["source"] == "loose_files"
    assert manifest["source_prefix"] == "tasks/task-loose/"
    assert {f["path"] for f in manifest["files"]} == {
        "task.toml",
        "instruction.md",
        "tests/test.sh",
    }
    assert all(
        f["source_key"].startswith("tasks/task-loose/") for f in manifest["files"]
    )


@pytest.mark.asyncio
async def test_expand_short_circuits_when_loose_manifest_already_exists(
    monkeypatch, _patched_get_session
):
    """Re-running expansion on a loose-file task that's already been
    migrated must not redo the copy work. Checked on manifest presence
    alone (pre-archive tasks are frozen)."""
    existing_manifest = json.dumps(
        {
            "task_id": "task-loose",
            "version": 1,
            "source": "loose_files",
            "source_prefix": "tasks/task-loose/",
            "files_count": 1,
            "files": [{"path": "task.toml", "size": 15, "source_key": "..."}],
        }
    ).encode("utf-8")
    storage = _FakeStorage(
        loose_objects={
            "tasks/task-loose/task.toml": b"name = 'loose'\n",
            "tasks/task-loose/v1-files/.oddish-manifest.json": existing_manifest,
        },
    )
    monkeypatch.setattr(task_expand_handler, "get_storage_client", lambda: storage)

    summary = await task_expand_handler.run_task_expand_job(
        task_id="task-loose", version=1
    )

    assert summary["status"] == "already_expanded"
    assert summary["source"] == "loose_files"
    assert storage.upload_calls == []


@pytest.mark.asyncio
async def test_expand_fails_when_task_has_neither_archive_nor_loose_files(
    monkeypatch, _patched_get_session
):
    """A task_version row whose S3 prefix is empty (archive deleted,
    no loose files) is genuinely unrecoverable. The job should raise so
    the worker records a failure rather than silently marking it
    expanded with zero files."""
    storage = _FakeStorage(loose_objects={})
    monkeypatch.setattr(task_expand_handler, "get_storage_client", lambda: storage)

    with pytest.raises(RuntimeError, match="no archive"):
        await task_expand_handler.run_task_expand_job(task_id="task-empty", version=1)


@pytest.mark.asyncio
async def test_expand_tolerates_per_file_upload_failures_in_archive_path(
    monkeypatch, _patched_get_session
):
    """A single rejected key (e.g. Supabase's S3 proxy bans ``[brackets]``
    or URL-encoded bytes) must not fail the whole task. Good members
    still land under ``v{N}-files/``; rejected ones are recorded as
    ``skipped: true`` in the manifest with an explanatory reason."""
    archive_bytes = _make_archive(
        {
            "task.toml": b"name = 'demo'\n",
            # Two files with characters Supabase's S3 proxy rejects.
            "tests/app/[subdomain]/page.tsx": b"export default () => null\n",
            "pages/%5Fhidden.md": b"hidden\n",
            # And a plain file that must still succeed.
            "solution/solve.sh": b"#!/bin/sh\n",
        }
    )
    storage = _FakeStorage(
        archive_key="tasks/task-brackets/v1/.oddish-task.tar.gz",
        archive_bytes=archive_bytes,
    )
    storage.reject_key_substrings = ("[", "%5F")
    monkeypatch.setattr(task_expand_handler, "get_storage_client", lambda: storage)

    summary = await task_expand_handler.run_task_expand_job(
        task_id="task-brackets", version=1
    )

    assert summary["status"] == "expanded"
    # 2 OK files + 2 rejected => 2 skipped, 2 real.
    assert summary["files"] == 2
    assert summary["skipped"] == 2

    expanded_prefix = "tasks/task-brackets/v1-files/"
    # OK files landed.
    assert f"{expanded_prefix}task.toml" in storage._objects
    assert f"{expanded_prefix}solution/solve.sh" in storage._objects
    # Rejected keys did NOT land.
    assert f"{expanded_prefix}tests/app/[subdomain]/page.tsx" not in storage._objects
    assert f"{expanded_prefix}pages/%5Fhidden.md" not in storage._objects

    manifest = json.loads(
        storage._objects[
            f"{expanded_prefix}{StorageClient._EXPANDED_MANIFEST_OBJECT_NAME}"
        ].decode("utf-8")
    )
    by_path = {f["path"]: f for f in manifest["files"]}
    assert by_path["tests/app/[subdomain]/page.tsx"]["skipped"] is True
    assert "InvalidKey" in by_path["tests/app/[subdomain]/page.tsx"]["skip_reason"]
    assert by_path["pages/%5Fhidden.md"]["skipped"] is True
    # Plain files carry a sha256 and no ``skipped`` flag.
    assert "sha256" in by_path["task.toml"]
    assert "skipped" not in by_path["task.toml"]


@pytest.mark.asyncio
async def test_expand_tolerates_per_file_upload_failures_in_loose_path(
    monkeypatch, _patched_get_session
):
    """Same tolerance guarantee for the loose-file migration path:
    a Supabase-rejected key in the source tree yields a skipped
    manifest entry, not a whole-task failure."""
    storage = _FakeStorage(
        loose_objects={
            "tasks/task-loose-brackets/task.toml": b"name = 'demo'\n",
            "tasks/task-loose-brackets/pages/[slug]/page.tsx": b"ok\n",
            "tasks/task-loose-brackets/instruction.md": b"do it\n",
        },
    )
    storage.reject_key_substrings = ("[",)
    monkeypatch.setattr(task_expand_handler, "get_storage_client", lambda: storage)

    summary = await task_expand_handler.run_task_expand_job(
        task_id="task-loose-brackets", version=1
    )

    assert summary["status"] == "expanded"
    assert summary["source"] == "loose_files"
    # summary["files"] counts ALL manifest entries, successful + skipped;
    # that's the same semantic as the archive path returns.
    expanded_prefix = "tasks/task-loose-brackets/v1-files/"
    # 2 files landed, the bracketed one didn't.
    assert f"{expanded_prefix}task.toml" in storage._objects
    assert f"{expanded_prefix}instruction.md" in storage._objects
    assert f"{expanded_prefix}pages/[slug]/page.tsx" not in storage._objects

    manifest = json.loads(
        storage._objects[
            f"{expanded_prefix}{StorageClient._EXPANDED_MANIFEST_OBJECT_NAME}"
        ].decode("utf-8")
    )
    by_path = {f["path"]: f for f in manifest["files"]}
    assert by_path["pages/[slug]/page.tsx"]["skipped"] is True
    assert "InvalidKey" in by_path["pages/[slug]/page.tsx"]["skip_reason"]


@pytest.mark.asyncio
async def test_expand_is_idempotent_on_matching_etag(monkeypatch, _patched_get_session):
    archive_bytes = _make_archive({"task.toml": b"name = 'demo'\n"})
    existing_manifest = json.dumps(
        {
            "task_id": "task-abc",
            "version": 1,
            "archive_etag": '"etag-v1"',
            "files_count": 1,
            "files": [{"path": "task.toml", "size": 15, "sha256": "deadbeef"}],
        }
    ).encode("utf-8")
    storage = _FakeStorage(
        archive_key="tasks/task-abc/v1/.oddish-task.tar.gz",
        archive_bytes=archive_bytes,
        manifest_bytes=existing_manifest,
    )
    monkeypatch.setattr(task_expand_handler, "get_storage_client", lambda: storage)

    summary = await task_expand_handler.run_task_expand_job(
        task_id="task-abc", version=1
    )

    assert summary["status"] == "already_expanded"
    # No member uploads on short-circuit: upload_calls stays empty because
    # the manifest etag already matches.
    assert storage.upload_calls == []


@pytest.mark.asyncio
async def test_expand_skips_oversize_archive(monkeypatch, _patched_get_session):
    archive_bytes = _make_archive({"task.toml": b"x" * 1024})
    storage = _FakeStorage(
        archive_key="tasks/task-abc/v1/.oddish-task.tar.gz",
        archive_bytes=archive_bytes,
    )
    monkeypatch.setattr(task_expand_handler, "get_storage_client", lambda: storage)
    # Pretend every archive is oversize.
    monkeypatch.setattr(settings, "tasks_expand_max_bytes", 1)

    # Spy on ``_mark_version_expanded`` so we can verify it's NOT called
    # on the oversize path (stamping ``expanded_at`` would permanently
    # hide the version from /admin/tasks/expand-backfill if the cap is
    # raised later).
    stamp_calls: list[dict] = []

    async def _spy_mark(**kwargs):
        stamp_calls.append(kwargs)

    monkeypatch.setattr(task_expand_handler, "_mark_version_expanded", _spy_mark)

    summary = await task_expand_handler.run_task_expand_job(
        task_id="task-abc", version=1
    )

    assert summary["status"] == "skipped"
    assert summary["reason"] == "archive_too_large"
    # Nothing written beyond the archive itself.
    assert storage.upload_calls == []
    # expanded_at must remain NULL so a future cap raise can re-pick
    # this version via the backfill endpoint.
    assert stamp_calls == []


@pytest.mark.asyncio
async def test_expand_skips_oversize_member(monkeypatch, _patched_get_session):
    archive_bytes = _make_archive(
        {
            "small.txt": b"ok\n",
            "big.bin": b"x" * 500,
        }
    )
    storage = _FakeStorage(
        archive_key="tasks/task-abc/v1/.oddish-task.tar.gz",
        archive_bytes=archive_bytes,
    )
    monkeypatch.setattr(task_expand_handler, "get_storage_client", lambda: storage)
    # Any member >10 bytes is skipped.
    monkeypatch.setattr(settings, "tasks_expand_max_member_bytes", 10)

    summary = await task_expand_handler.run_task_expand_job(
        task_id="task-abc", version=1
    )

    assert summary["status"] == "expanded"
    # The small file must be uploaded; the big one must not.
    expanded_prefix = "tasks/task-abc/v1-files/"
    assert f"{expanded_prefix}small.txt" in storage._objects
    assert f"{expanded_prefix}big.bin" not in storage._objects

    manifest = json.loads(
        storage._objects[
            f"{expanded_prefix}{StorageClient._EXPANDED_MANIFEST_OBJECT_NAME}"
        ].decode("utf-8")
    )
    by_path = {f["path"]: f for f in manifest["files"]}
    assert by_path["big.bin"].get("skipped") is True
    assert by_path["small.txt"].get("skipped") is None or not by_path["small.txt"].get(
        "skipped"
    )


@pytest.mark.asyncio
async def test_expand_single_pass_extracts_every_member_correctly(
    monkeypatch, _patched_get_session
):
    """A previous implementation re-opened the tarball per member
    (O(N^2) decompression). The streaming extractor must still produce
    exact per-member bytes for every file in the archive."""
    files = {
        f"dir/file_{i:03d}.txt": (f"content-{i}\n" * (i + 1)).encode("utf-8")
        for i in range(25)
    }
    archive_bytes = _make_archive(files)
    storage = _FakeStorage(
        archive_key="tasks/task-abc/v1/.oddish-task.tar.gz",
        archive_bytes=archive_bytes,
    )
    monkeypatch.setattr(task_expand_handler, "get_storage_client", lambda: storage)

    summary = await task_expand_handler.run_task_expand_job(
        task_id="task-abc", version=1
    )

    assert summary["status"] == "expanded"
    assert summary["files"] == len(files)

    expanded_prefix = "tasks/task-abc/v1-files/"
    for path, expected in files.items():
        assert storage._objects[f"{expanded_prefix}{path}"] == expected


def test_extract_regular_members_reads_every_body_in_one_pass():
    """Unit test for the streaming extractor: exhaust an archive in a
    single tarfile-open and confirm we get the full bytes for every
    member (the bug the refactor fixes would surface as truncated or
    swapped bodies)."""
    files = {
        "alpha.txt": b"aaaaaaaaaaaaaaaaaaaa",
        "beta.txt": b"bbbb",
        "nested/gamma.txt": b"gamma-" * 32,
    }
    archive_bytes = _make_archive(files)

    members = task_expand_handler._extract_regular_members(
        archive_bytes, max_member_bytes=0
    )

    by_name = {str(m["name"]): m for m in members}
    assert set(by_name) == set(files)
    for name, payload in files.items():
        assert by_name[name]["body"] == payload
        assert by_name[name]["size"] == len(payload)
        assert by_name[name]["skipped"] is False


def test_extract_regular_members_marks_oversize_members_skipped():
    files = {
        "small.txt": b"ok",
        "big.bin": b"x" * 200,
    }
    archive_bytes = _make_archive(files)

    members = task_expand_handler._extract_regular_members(
        archive_bytes, max_member_bytes=10
    )

    by_name = {str(m["name"]): m for m in members}
    assert by_name["small.txt"]["skipped"] is False
    assert by_name["small.txt"]["body"] == b"ok"
    assert by_name["big.bin"]["skipped"] is True
    # Skipped entries must not carry their body into memory.
    assert by_name["big.bin"]["body"] == b""


@pytest.mark.asyncio
async def test_expand_marks_failed_on_corrupt_tar(monkeypatch, _patched_get_session):
    storage = _FakeStorage(
        archive_key="tasks/task-abc/v1/.oddish-task.tar.gz",
        archive_bytes=b"not a valid gzip tar",
    )
    monkeypatch.setattr(task_expand_handler, "get_storage_client", lambda: storage)

    with pytest.raises(Exception):
        await task_expand_handler.run_task_expand_job(task_id="task-abc", version=1)

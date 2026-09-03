"""Task-file reads with the database's expansion answer in hand.

``resolve_task_file_source`` returns whether the per-file tree under
``v{N}-files/`` was built from the selected archive. With that answer the
storage layer skips the manifest HEAD + GET (and the presence HEAD before a
read); without it (``None``) the probing behaviour in ``test_task_storage``
is unchanged.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from oddish.db import storage as storage_mod


def _missing_object() -> ClientError:
    return ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")


class _Probes:
    """Records every storage probe the read path issues."""

    def __init__(self, storage, *, existing: set[str], texts: dict[str, str]):
        self.calls: list[tuple[str, str]] = []
        self.existing = existing
        self.texts = texts
        storage._client = object()
        for name in ("object_exists", "download_json", "download_text"):
            setattr(storage, name, getattr(self, name))

    async def object_exists(self, key: str) -> bool:
        self.calls.append(("HEAD", key))
        return key in self.existing

    async def download_json(self, key: str) -> dict:
        self.calls.append(("GET", key))
        raise AssertionError(f"manifest must not be read: {key}")

    async def download_text(self, key: str) -> str:
        self.calls.append(("GET", key))
        if key not in self.texts:
            raise _missing_object()
        return self.texts[key]


@pytest.mark.asyncio
async def test_listing_trusts_the_database_and_skips_every_probe(monkeypatch):
    storage = storage_mod.StorageClient()
    probes = _Probes(storage, existing=set(), texts={})

    async def fake_list_objects_all(prefix: str, **_: object) -> list[dict]:
        assert prefix == "tasks/t1/v2-files/"
        return [
            {"key": f"{prefix}task.toml", "size": 4, "last_modified": None},
            {"key": f"{prefix}.oddish-manifest.json", "size": 9, "last_modified": None},
        ]

    monkeypatch.setattr(storage, "list_objects_all", fake_list_objects_all)

    listing = await storage.list_task_files(
        task_id="t1",
        prefix=None,
        recursive=True,
        limit=1000,
        cursor=None,
        presign=False,
        version=2,
        task_s3_prefix="tasks/t1/v2/",
        inline=False,
        expanded=True,
    )

    assert [f["path"] for f in listing["files"]] == ["task.toml"]
    assert probes.calls == []


@pytest.mark.asyncio
async def test_listing_with_a_negative_answer_never_looks_for_the_manifest(monkeypatch):
    storage = storage_mod.StorageClient()
    archive_key = "tasks/t1/v2/.oddish-task.tar.gz"
    probes = _Probes(storage, existing={archive_key}, texts={})

    async def fake_load_task_archive(key: str):
        assert key == archive_key
        return b"", [{"path": "task.toml", "size": 4}], {"task.toml": "x = 1"}

    monkeypatch.setattr(storage, "_load_task_archive", fake_load_task_archive)

    listing = await storage.list_task_files(
        task_id="t1",
        prefix=None,
        recursive=True,
        limit=1000,
        cursor=None,
        presign=False,
        version=2,
        task_s3_prefix="tasks/t1/v2/",
        expanded=False,
    )

    assert [f["path"] for f in listing["files"]] == ["task.toml"]
    assert probes.calls == [("HEAD", archive_key)]


@pytest.mark.asyncio
async def test_content_reads_the_member_directly_when_the_database_vouches():
    storage = storage_mod.StorageClient()
    member_key = "tasks/t1/v2-files/task.toml"
    probes = _Probes(storage, existing=set(), texts={member_key: "x = 1\n"})

    result = await storage.get_task_file_content(
        task_id="t1",
        file_path="task.toml",
        presign=False,
        version=2,
        task_s3_prefix="tasks/t1/v2/",
        expanded=True,
    )

    assert result == {
        "path": "task.toml",
        "content": "x = 1\n",
        "is_truncated": False,
        "key": member_key,
    }
    assert probes.calls == [("GET", member_key)]


@pytest.mark.asyncio
async def test_content_falls_back_to_the_archive_for_a_member_the_tree_skipped(
    monkeypatch,
):
    storage = storage_mod.StorageClient()
    archive_key = "tasks/t1/v2/.oddish-task.tar.gz"
    member_key = "tasks/t1/v2-files/huge.bin"
    probes = _Probes(storage, existing={archive_key}, texts={})

    async def fake_load_task_archive(key: str):
        assert key == archive_key
        return b"", [{"path": "huge.bin", "size": 3}], {"huge.bin": "big"}

    async def fake_head_archive_etag(key: str) -> str:
        return "etag-1"

    monkeypatch.setattr(storage, "_load_task_archive", fake_load_task_archive)
    monkeypatch.setattr(storage, "_head_archive_etag", fake_head_archive_etag)

    result = await storage.get_task_file_content(
        task_id="t1",
        file_path="huge.bin",
        presign=False,
        version=2,
        task_s3_prefix="tasks/t1/v2/",
        expanded=True,
    )

    assert result["content"] == "big"
    assert result["archive_key"] == archive_key
    assert result["archive_etag"] == "etag-1"
    # One failed read, then the archive: still no manifest traffic.
    assert probes.calls == [("GET", member_key), ("HEAD", archive_key)]


@pytest.mark.asyncio
async def test_presigned_url_still_checks_presence_because_it_cannot_fall_back(
    monkeypatch,
):
    storage = storage_mod.StorageClient()
    member_key = "tasks/t1/v2-files/task.toml"
    probes = _Probes(storage, existing={member_key}, texts={})

    async def fake_presign(key: str, expiration: int) -> str:
        return f"https://signed/{key}"

    monkeypatch.setattr(storage, "get_presigned_url", fake_presign)

    result = await storage.get_task_file_content(
        task_id="t1",
        file_path="task.toml",
        presign=True,
        version=2,
        task_s3_prefix="tasks/t1/v2/",
        expanded=True,
    )

    assert result["url"] == f"https://signed/{member_key}"
    assert probes.calls == [("HEAD", member_key)]


@pytest.mark.asyncio
async def test_list_objects_all_stops_paginating_at_the_cap():
    storage = storage_mod.StorageClient()
    pages_served: list[int] = []

    class _Paginator:
        async def paginate(self, **_: object):
            for page_number in range(5):
                pages_served.append(page_number)
                yield {
                    "Contents": [
                        {"Key": f"p{page_number}/k{i}", "Size": 1, "LastModified": None}
                        for i in range(3)
                    ]
                }

    class _Client:
        def get_paginator(self, name: str) -> _Paginator:
            assert name == "list_objects_v2"
            return _Paginator()

    storage._client = _Client()

    objects = await storage.list_objects_all("p", max_keys=4)

    assert len(objects) == 5  # cap plus one: the caller can tell it was cut
    assert pages_served == [0, 1]

    everything = await storage.list_objects_all("p")
    assert len(everything) == 15


@pytest.mark.asyncio
async def test_recursive_trial_listing_is_capped_and_says_so(monkeypatch):
    storage = storage_mod.StorageClient()
    storage._client = object()
    root = "trials/tr-1/"

    async def fake_list_objects_all(prefix: str, *, max_keys=None) -> list[dict]:
        assert prefix == root and max_keys == 2
        return [
            {"key": f"{root}f{i}", "size": 1, "last_modified": None} for i in range(3)
        ]

    monkeypatch.setattr(storage, "list_objects_all", fake_list_objects_all)
    monkeypatch.setattr(storage, "_trial_prefix", staticmethod(lambda trial_id: root))

    listing = await storage.list_trial_files(
        trial_id="tr-1",
        prefix=None,
        recursive=True,
        limit=2,
        cursor=None,
        presign=False,
    )

    assert [f["path"] for f in listing["files"]] == ["f0", "f1"]
    assert listing["truncated"] is True

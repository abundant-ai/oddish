from __future__ import annotations

import io
import tarfile

from botocore.exceptions import ClientError
import pytest

from oddish.db.storage import StorageClient
from oddish.timing import begin_request_timing, reset_request_timing


class Body:
    def __init__(self, data):
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def read(self):
        return self.data


class MemoryS3:
    def __init__(self, objects):
        self.objects = objects
        self.calls = []

    async def head_object(self, *, Bucket, Key):
        self.calls.append(("HEAD", Key))
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")
        return {"ETag": '"test"', "ContentLength": len(self.objects[Key])}

    async def get_object(self, *, Bucket, Key, **options):
        self.calls.append(("GET", Key))
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        body = self.objects[Key]
        if "Range" in options:
            body = body[: int(options["Range"].split("-")[-1]) + 1]
        return {"Body": Body(body)}


@pytest.fixture
def storage():
    StorageClient._archive_cache.clear()
    StorageClient._archive_cache_bytes = 0
    StorageClient._archive_etag_hints.clear()
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tf:
        data = b"task instruction"
        member = tarfile.TarInfo("instruction.md")
        member.size = len(data)
        tf.addfile(member, io.BytesIO(data))
    client = StorageClient()
    client._client = MemoryS3({"tasks/t/v1/.oddish-task.tar.gz": archive.getvalue()})
    yield client
    StorageClient._archive_cache.clear()
    StorageClient._archive_cache_bytes = 0
    StorageClient._archive_etag_hints.clear()


@pytest.mark.asyncio
async def test_archive_read_reuses_head_and_reports_cache_and_bytes(storage):
    for cached, expected_operations in [(False, ["HEAD", "GET"]), (True, ["HEAD"])]:
        storage._client.calls.clear()
        timing, token, stage = begin_request_timing()
        try:
            result = await storage.get_task_file_content(
                task_id="t",
                version=1,
                task_s3_prefix="tasks/t/v1/",
                expanded=False,
                file_path="instruction.md",
                presign=False,
            )
            assert result["content"] == "task instruction"
            assert timing.file_source == "archive"
            assert timing.file_bytes == len(b"task instruction")
            assert timing.archive_cache_hit is cached
            assert timing.archive_bytes > timing.file_bytes
            assert timing.storage_request_count == len(expected_operations)
            assert [call[0] for call in storage._client.calls] == expected_operations
            if cached:
                assert timing.storage_bytes == 0
                assert "archive_parse" not in timing.durations_ms
            else:
                assert timing.storage_bytes == timing.archive_bytes
                assert {
                    "storage_head",
                    "storage_get",
                    "storage_read",
                    "archive_parse",
                } <= timing.durations_ms.keys()
        finally:
            reset_request_timing((token, stage))


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_published_file_uses_one_get_and_missing_member_falls_back(
    storage, missing
):
    prefix = "tasks/t/v1-expanded/published/"
    if not missing:
        storage._client.objects[prefix + "instruction.md"] = b"task instruction"
    timing, token, stage = begin_request_timing()
    try:
        result = await storage.get_task_file_content(
            task_id="t",
            version=1,
            task_s3_prefix="tasks/t/v1/",
            expanded=True,
            expanded_manifest_key=prefix + ".oddish-manifest.json",
            file_path="instruction.md",
            presign=False,
        )
        assert result["content"] == "task instruction"
        assert timing.file_source == ("archive" if missing else "expanded")
        assert [call[0] for call in storage._client.calls] == (
            ["GET", "HEAD", "GET"] if missing else ["GET"]
        )
    finally:
        reset_request_timing((token, stage))


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["head", "delete"])
async def test_provider_error_retains_safe_diagnostics(storage, caplog, operation):
    async def rejected(**kwargs):
        raise ClientError(
            {
                "Error": {"Code": "BadDigest", "Message": "Checksum mismatch"},
                "ResponseMetadata": {
                    "HTTPStatusCode": 400,
                    "RequestId": "provider-request",
                    "RetryAttempts": 2,
                    "HTTPHeaders": {"authorization": "never-log-this"},
                },
            },
            "HeadObject" if operation == "head" else "DeleteObjects",
        )

    key = "tasks/t/v1/.oddish-task.tar.gz"
    if operation == "head":
        storage._client.head_object = rejected
    else:

        async def listed(_prefix):
            return [key]

        storage.list_keys = listed
        storage._client.delete_objects = rejected
    with pytest.raises(ClientError):
        if operation == "head":
            await storage.head_object(key)
        else:
            await storage.delete_prefix("tasks/t/v1/")
    assert (
        "status=400 code=BadDigest request_id=provider-request retries=2" in caplog.text
    )
    assert "Checksum mismatch" in caplog.text
    assert "never-log-this" not in caplog.text
    if operation == "delete":
        assert "batch_size=1" in caplog.text


@pytest.mark.asyncio
async def test_listing_helpers_preserve_source_hash_in_json_and_stream(
    storage, monkeypatch
):
    from oddish.core.sharing import helpers

    monkeypatch.setattr(helpers, "get_storage_client", lambda: storage)
    arguments = dict(
        task_id="t",
        version=1,
        task_s3_prefix="tasks/t/v1/",
        prefix=None,
        recursive=True,
        limit=1000,
        cursor=None,
        presign=False,
        expanded=False,
        source_hash="version-hash",
    )
    listing = await helpers.list_task_files_s3(**arguments)
    chunks = [chunk async for chunk in helpers.stream_task_files_s3(**arguments)]
    assert listing["source_hash"] == chunks[0]["source_hash"] == "version-hash"
    assert chunks[0]["type"] == "listing"
    assert any(chunk.get("content") == "task instruction" for chunk in chunks[1:])


@pytest.mark.asyncio
async def test_published_presign_checks_member_and_preserves_missing_fallback(storage):
    prefix = "tasks/t/v1-expanded/published/"
    storage._client.objects[prefix + "instruction.md"] = b"task instruction"

    async def signed(key, expiration=900):
        return f"https://storage.invalid/{key}?expires={expiration}"

    storage.get_presigned_url = signed
    result = await storage.get_task_file_content(
        task_id="t",
        version=1,
        task_s3_prefix="tasks/t/v1/",
        expanded=True,
        expanded_manifest_key=prefix + ".oddish-manifest.json",
        file_path="instruction.md",
        presign=True,
    )
    assert result["url"].startswith(f"https://storage.invalid/{prefix}instruction.md")
    assert storage._client.calls == [("HEAD", prefix + "instruction.md")]


@pytest.mark.asyncio
async def test_old_published_snapshot_remains_readable_after_overwrite(storage):
    # Requests select their archive and expansion together. A newer request's
    # pointer must not replace the bytes an earlier request already selected.
    old = "tasks/t/v1-expanded/old/"
    new = "tasks/t/v1-expanded/new/"
    storage._client.objects.update(
        {old + "instruction.md": b"old", new + "instruction.md": b"new"}
    )
    for prefix, expected in [(new, "new"), (old, "old")]:
        result = await storage.get_task_file_content(
            task_id="t",
            version=1,
            task_s3_prefix="tasks/t/v1/",
            expanded=True,
            expanded_manifest_key=prefix + ".oddish-manifest.json",
            file_path="instruction.md",
            presign=False,
        )
        assert result["content"] == expected

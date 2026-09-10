"""Expansion hints must preserve archive identity and complete trial downloads."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from oddish.cli.pull import _pull_trial
from oddish.db.storage import StorageClient


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["listing", "content", "presign"])
@pytest.mark.parametrize(
    "expanded, manifest_archive",
    [(True, "old"), (True, "new"), (True, None), (False, "new"), (None, "new")],
)
async def test_expansion_hint_preserves_selected_archive(
    expanded, manifest_archive, operation
):
    storage = StorageClient()
    storage._client = object()
    storage._inline_object_contents = AsyncMock(side_effect=lambda files: files)
    archive = "tasks/t1/old/.oddish-task.tar.gz"
    tree = "tasks/t1/v1-files/"
    manifest = f"{tree}.oddish-manifest.json"
    existing = {archive, f"{tree}instruction.md"}
    if manifest_archive is not None:
        existing.add(manifest)
    storage.head_object = AsyncMock(
        side_effect=lambda key: {"ETag": "old-etag"} if key in existing else None
    )
    storage.download_json = AsyncMock(
        return_value={
            "archive_key": f"tasks/t1/{manifest_archive}/.oddish-task.tar.gz",
        }
    )
    # The request already selected the old archive. An overwrite may have
    # replaced (or be rebuilding) v1-files before its storage read begins.
    storage.download_text = AsyncMock(return_value="expanded content")
    storage.list_objects_all = AsyncMock(
        return_value=[
            {"key": f"{tree}instruction.md", "size": 16},
        ]
    )
    storage.get_presigned_url = AsyncMock(return_value="https://signed/tree")
    storage._load_task_archive = AsyncMock(
        return_value=(
            b"",
            [{"path": "instruction.md", "size": 11}],
            {"instruction.md": "old archive"},
        )
    )
    storage._head_archive_etag = AsyncMock(return_value="old-etag")
    params = dict(
        task_id="t1", version=1, task_s3_prefix="tasks/t1/old/", expanded=expanded
    )
    if operation == "listing":
        result = await storage.list_task_files(
            **params,
            prefix=None,
            recursive=True,
            limit=1000,
            cursor=None,
            presign=False,
        )
        result = result["files"][0]
    else:
        result = await storage.get_task_file_content(
            **params,
            file_path="instruction.md",
            presign=operation == "presign",
        )
    if manifest_archive == "old":
        assert result["key"] == f"{tree}instruction.md"
        storage._load_task_archive.assert_not_awaited()
    else:
        assert result["content"] == "old archive"
        if operation != "listing":
            assert result["key"] == f"{archive}#instruction.md"
        storage._load_task_archive.assert_awaited_once_with(
            archive, head={"ETag": "old-etag"}
        )
        storage.download_text.assert_not_awaited()
    if expanded is False:
        storage.download_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_pull_saves_all_files_across_storage_pages(tmp_path):
    storage = StorageClient()
    root = "trials/review-trial/"
    keys = [f"{root}artifact-{i:04d}.txt" for i in range(1001)]

    class Paginator:
        async def paginate(self, **kwargs):
            assert kwargs["Prefix"] == root
            for start in range(0, len(keys), 1000):
                yield {
                    "Contents": [
                        {"Key": key, "Size": 1} for key in keys[start : start + 1000]
                    ]
                }

    class Client:
        def get_paginator(self, name):
            assert name == "list_objects_v2"
            return Paginator()

    storage._client = Client()
    listing = await storage.list_trial_files(
        trial_id="review-trial",
        root_prefix=root,
        prefix=None,
        recursive=True,
        limit=1000,
        cursor=None,
        presign=False,
    )

    def handle(request):
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json=listing)
        if "/files/" in request.url.path:
            return httpx.Response(200, content=b"x")
        return httpx.Response(200, json={})

    with httpx.Client(
        base_url="http://review", transport=httpx.MockTransport(handle)
    ) as client:
        summary = _pull_trial(
            client,
            "review-trial",
            tmp_path,
            include_logs=False,
            include_files=True,
            include_structured_logs=False,
        )
    assert summary["errors"] == 0
    assert summary["files_saved"] == len(keys)
    assert (tmp_path / "trials/review-trial/artifact-1000.txt").read_text() == "x"

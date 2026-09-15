from uuid import uuid4
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import delete

from oddish.core.file_index import (
    publish_file_index,
    read_file_index,
    directory_entries,
    trial_index_key,
)
from oddish.db import get_session
from oddish.db.models import FileIndexModel


@pytest_asyncio.fixture
async def source():
    key = "test-index:" + uuid4().hex
    yield key
    async with get_session() as session:
        await session.execute(
            delete(FileIndexModel).where(FileIndexModel.source_key == key)
        )
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("unrelated_count", [1, 10000])
async def test_directory_and_artifact_pages_are_bounded(source, unrelated_count):
    files = [
        dict(path=f"logs/trace-{i:05}.txt", size=200) for i in range(unrelated_count)
    ]
    files += [dict(path=f"artifacts/report-{i:03}.txt", size=300) for i in range(105)]
    async with get_session() as session:
        await publish_file_index(
            session, source_key=source, root_prefix="test/", files=files
        )
        await session.commit()
    root = await read_file_index(source_key=source, directories=["", "logs"], limit=100)
    assert root["directories"][""]["files"] == []
    assert root["directories"][""]["dirs"] == [{"path": "artifacts"}, {"path": "logs"}]
    assert len(root["directories"]["logs"]["files"]) == min(unrelated_count, 100)
    page = await read_file_index(source_key=source, artifacts=True, limit=100)
    assert len(page["files"]) == 100
    assert page["truncated"] is True
    assert all("artifacts/" in f["path"] for f in page["files"])
    next_page = await read_file_index(
        source_key=source, artifacts=True, cursor=page["cursor"], limit=100
    )
    assert len(next_page["files"]) == 5
    assert next_page["cursor"] is None
    assert not {f["path"] for f in page["files"]} & {
        f["path"] for f in next_page["files"]
    }


@pytest.mark.asyncio
async def test_nested_inventory_publication_uses_bounded_insert_batches(source):
    from sqlalchemy import event
    from oddish.db.connection import engine

    # Directory sizes are NULL; file sizes are integers. Alternating them must
    # not turn one inventory batch into a database round trip for every entry.
    files = [dict(path=f"folder-{i:03}/file.txt", size=i) for i in range(300)]
    inserts = []

    def record_insert(_conn, _cursor, statement, _params, _context, _many):
        if statement.startswith("INSERT INTO file_entries"):
            inserts.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record_insert)
    try:
        async with get_session() as session:
            await publish_file_index(
                session, source_key=source, root_prefix="test/", files=files
            )
            await session.commit()
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_insert)

    assert len(inserts) == 2  # 600 entries, published in batches of 500.
    page = await read_file_index(source_key=source, prefix="folder-000")
    assert page["files"][0]["size"] == 0
    root = await read_file_index(source_key=source, limit=1000)
    assert len(root["dirs"]) == 300


@pytest.mark.asyncio
async def test_publication_rollback_preserves_complete_inventory(source):
    async with get_session() as session:
        await publish_file_index(
            session,
            source_key=source,
            root_prefix="old/",
            files=[{"path": "a", "size": 1}],
        )
        await session.commit()
    before = await read_file_index(source_key=source)
    async with get_session() as session:
        await publish_file_index(
            session,
            source_key=source,
            root_prefix="new/",
            files=[{"path": "b", "size": 2}],
        )
        await session.rollback()
    assert await read_file_index(source_key=source) == before


@pytest.mark.asyncio
async def test_prepared_task_listing_cannot_read_storage(source, monkeypatch):
    from oddish.core.sharing import helpers

    async with get_session() as session:
        await publish_file_index(
            session,
            source_key=source,
            root_prefix="test/",
            files=[{"path": "instruction.md", "size": 3}],
        )
        await session.commit()
    monkeypatch.setattr(
        helpers,
        "get_storage_client",
        lambda: pytest.fail("directory request must not access storage"),
    )
    response = await helpers.list_task_files_s3(
        task_id="task",
        prefix=None,
        recursive=False,
        limit=100,
        cursor=None,
        presign=False,
        inline=False,
        task_s3_prefix=None,
        expanded_manifest_key=source,
        directories=[""],
        indexed=True,
    )
    assert response["directories"][""]["files"][0]["path"] == "instruction.md"


@pytest.mark.asyncio
async def test_missing_index_is_retryable_not_a_false_empty_listing(source):
    with pytest.raises(HTTPException) as exc:
        await read_file_index(source_key=source)
    assert exc.value.status_code == 503
    assert exc.value.headers["Retry-After"] == "2"


def test_artifact_classification_happens_at_publication():
    entries = directory_entries(
        [{"path": "trial/steps/setup/artifacts/report.txt", "size": 3}]
    )
    assert [e["path"] for e in entries if e["artifact"]] == [
        "trial/steps/setup/artifacts/report.txt"
    ]


@pytest.mark.asyncio
async def test_trial_preview_uses_exact_index_and_storage_byte_bound(
    source, monkeypatch
):
    from oddish.core.sharing import helpers

    trial = SimpleNamespace(id=source, attempts=2, trial_s3_key="attempt-2/")
    index_key = trial_index_key(trial)
    async with get_session() as session:
        await publish_file_index(
            session,
            source_key=index_key,
            root_prefix="attempt-2/selected-child/",
            files=[{"path": "artifacts/a.txt", "size": 200000}],
        )
        await session.commit()
    storage = SimpleNamespace(download_bytes=AsyncMock(return_value=b"abc"))
    monkeypatch.setattr(helpers, "get_storage_client", lambda: storage)
    monkeypatch.setattr(
        helpers,
        "resolve_trial_artifact_layout",
        AsyncMock(side_effect=AssertionError("must not rediscover layout")),
    )
    try:
        content, _ = await helpers.get_trial_file_content_s3(
            trial, "artifacts/a.txt", max_bytes=102400, indexed=True
        )
        assert content == b"abc"
        storage.download_bytes.assert_awaited_once_with(
            "attempt-2/selected-child/artifacts/a.txt", max_bytes=102400
        )
    finally:
        async with get_session() as session:
            await session.execute(
                delete(FileIndexModel).where(FileIndexModel.source_key == index_key)
            )
            await session.commit()


@pytest.mark.asyncio
async def test_old_attempt_or_inventory_revision_cannot_be_read_as_current(source):
    trial = SimpleNamespace(id="trial", attempts=2, trial_s3_key="attempt-2/")
    with pytest.raises(HTTPException) as exc:
        trial_index_key(trial, attempt=1)
    assert exc.value.status_code == 409
    async with get_session() as session:
        await publish_file_index(
            session,
            source_key=source,
            root_prefix="test/",
            files=[{"path": "a.txt", "size": 1}],
        )
        await session.commit()
    with pytest.raises(HTTPException) as exc:
        await read_file_index(source_key=source, revision="superseded-index")
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_task_pointer_and_index_publish_in_same_transaction():
    import json
    from oddish.db import TaskModel, TaskVersionModel
    from oddish.workers.queue.task_expand_handler import _promote_expansion_if_current
    from sqlalchemy import text

    task_id = "publish-" + uuid4().hex[:16]
    manifest_key = f"tasks/{task_id}/v1-expanded/{uuid4().hex}/.oddish-manifest.json"
    version_id = task_id + "-v1"
    storage = SimpleNamespace(upload_bytes=AsyncMock())
    async with get_session() as session:
        session.add(
            TaskModel(id=task_id, name=task_id, user="tester", task_path="s3://test")
        )
        await session.flush()
        session.add(
            TaskVersionModel(
                id=version_id,
                task_id=task_id,
                version=1,
                task_path="s3://test",
                content_hash="current",
            )
        )
        await session.commit()
    try:
        assert not await _promote_expansion_if_current(
            storage,
            task_id=task_id,
            version=1,
            expected_content_hash="old",
            manifest_key=manifest_key,
            manifest_bytes=json.dumps({"files": []}).encode(),
        )
        async with get_session() as session:
            assert await session.get(FileIndexModel, manifest_key) is None
        assert await _promote_expansion_if_current(
            storage,
            task_id=task_id,
            version=1,
            expected_content_hash="current",
            manifest_key=manifest_key,
            manifest_bytes=json.dumps(
                {"files": [{"path": "instruction.md", "size": 10}]}
            ).encode(),
        )
        async with get_session() as session:
            version = await session.get(TaskVersionModel, version_id)
            assert version.expanded_manifest_key == manifest_key
            index = await session.get(FileIndexModel, manifest_key)
            assert index.revision
            assert index.task_version_id == version_id
            from oddish.db import utcnow

            version.deleted_at = utcnow()
            await session.flush()
            version.deleted_at = None
            await session.flush()
            await session.commit()
        assert (await read_file_index(source_key=manifest_key))["files"][0][
            "path"
        ] == "instruction.md"
    finally:
        async with get_session() as session:
            await session.execute(
                text("DELETE FROM task_versions WHERE id=:id"), {"id": version_id}
            )
            await session.execute(
                text("DELETE FROM tasks WHERE id=:id"), {"id": task_id}
            )
            await session.commit()


@pytest.mark.asyncio
async def test_failed_historical_index_is_durable_and_retried(monkeypatch):
    from datetime import timedelta
    from oddish.db import TaskModel, TaskVersionModel, utcnow
    from oddish.core.file_index import backfill_file_indexes
    from sqlalchemy import text

    task_id = "backfill-" + uuid4().hex[:16]
    version_id = task_id + "-v1"
    key = f"tasks/{task_id}/v1-expanded/{uuid4().hex}/.oddish-manifest.json"
    storage = SimpleNamespace(
        download_json=AsyncMock(side_effect=OSError("simulated storage outage"))
    )
    import oddish.db

    monkeypatch.setattr(oddish.db, "get_storage_client", lambda: storage)
    async with get_session() as session:
        session.add(
            TaskModel(id=task_id, name=task_id, user="tester", task_path="s3://test")
        )
        await session.flush()
        session.add(
            TaskVersionModel(
                id=version_id,
                task_id=task_id,
                version=1,
                task_path="s3://test",
                expanded_manifest_key=key,
            )
        )
        await session.commit()
    try:
        await backfill_file_indexes()
        async with get_session() as session:
            marker = await session.get(FileIndexModel, key)
            assert marker.revision is None
            assert marker.next_attempt_at > utcnow()
            marker.next_attempt_at = utcnow() - timedelta(seconds=1)
            await session.commit()
        storage.download_json.side_effect = None
        storage.download_json.return_value = {
            "files": [{"path": "readme.txt", "size": 1}]
        }
        await backfill_file_indexes()
        assert (await read_file_index(source_key=key))["files"][0][
            "path"
        ] == "readme.txt"
    finally:
        async with get_session() as session:
            await session.execute(
                text("DELETE FROM task_versions WHERE id=:id"), {"id": version_id}
            )
            await session.execute(
                text("DELETE FROM tasks WHERE id=:id"), {"id": task_id}
            )
            await session.commit()


@pytest.mark.asyncio
async def test_bounded_storage_read_handles_partial_network_chunks():
    from oddish.db.storage import StorageClient

    class Body:
        read = AsyncMock(side_effect=[b"a", b"bc"])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    body = Body()
    storage = StorageClient.__new__(StorageClient)
    storage._ensure_client = AsyncMock()
    storage._client = SimpleNamespace(get_object=AsyncMock(return_value={"Body": body}))
    assert await storage.download_bytes("test.txt", max_bytes=3) == b"abc"
    assert storage._s3.get_object.call_args.kwargs["Range"] == "bytes=0-2"
    assert [call.args for call in body.read.call_args_list] == [(3,), (2,)]


@pytest.mark.asyncio
async def test_writer_replaces_early_inventory_and_late_backfill_cannot_restore_it(
    source,
):
    async with get_session() as session:
        await publish_file_index(
            session,
            source_key=source,
            root_prefix="test/",
            files=[{"path": "result.json", "size": 2}],
            only_if_pending=True,
        )
        await session.commit()
    early = await read_file_index(source_key=source)
    async with get_session() as session:
        await publish_file_index(
            session,
            source_key=source,
            root_prefix="test/",
            files=[{"path": "result.json", "size": 2}, {"path": "late.txt", "size": 3}],
        )
        await session.commit()
    complete = await read_file_index(source_key=source)
    assert complete["source_hash"] != early["source_hash"]
    assert {f["path"] for f in complete["files"]} == {"result.json", "late.txt"}
    async with get_session() as session:
        await publish_file_index(
            session,
            source_key=source,
            root_prefix="test/",
            files=[{"path": "result.json", "size": 2}],
            only_if_pending=True,
        )
        await session.commit()
    assert await read_file_index(source_key=source) == complete

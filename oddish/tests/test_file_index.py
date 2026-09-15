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
        index_key=source,
        directories=[""],
        indexed=True,
    )
    assert response["directories"][""]["files"][0]["path"] == "instruction.md"


@pytest.mark.asyncio
async def test_pending_index_is_retryable_not_a_false_empty_listing(source):
    async with get_session() as session:
        session.add(FileIndexModel(source_key=source))
        await session.commit()
    with pytest.raises(HTTPException) as exc:
        await read_file_index(source_key=source)
    assert exc.value.status_code == 503
    assert exc.value.headers["Retry-After"] == "2"


@pytest.mark.asyncio
async def test_absent_index_does_not_claim_preparation_is_running(source):
    with pytest.raises(HTTPException) as exc:
        await read_file_index(source_key=source)
    assert exc.value.status_code == 404
    assert exc.value.detail == "No file directory is available for this source"


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


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", ["archive", "directory", "canonical"])
async def test_versionless_sources_are_queued_and_indexed_without_version_changes(
    monkeypatch, layout
):
    import io
    import tarfile
    from datetime import datetime, UTC
    import oddish.db
    from oddish.core.file_index import backfill_file_indexes
    from oddish.core.task_files import resolve_task_file_source
    from oddish.core.sharing import helpers
    from oddish.db import TaskModel, TrialModel, TaskVersionModel, ExperimentModel
    from oddish.db.storage import StorageClient
    from sqlalchemy import select, text

    task_id = "legacy-" + uuid4().hex[:16]
    pointer = None if layout == "canonical" else f"legacy-imports/{task_id}/"
    root = pointer or f"tasks/{task_id}/"
    content = b"legacy instruction\n"
    files = [{"path": "instruction.md", "size": len(content)}]
    storage = StorageClient.__new__(StorageClient)
    archive_key = root + ".oddish-task.tar.gz"
    if layout == "archive":
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            member = tarfile.TarInfo("instruction.md")
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        storage.head_object = AsyncMock(return_value={"ETag": task_id})
        storage.download_bytes = AsyncMock(return_value=buffer.getvalue())
    else:
        storage.head_object = AsyncMock(return_value=None)
        storage.download_bytes = AsyncMock(return_value=content)
    storage.list_objects_all = AsyncMock(
        return_value=[{"key": root + f["path"], "size": f["size"]} for f in files]
    )
    monkeypatch.setattr(oddish.db, "get_storage_client", lambda: storage)
    async with get_session() as session:
        task = TaskModel(
            id=task_id,
            name=task_id,
            user="tester",
            task_path="legacy",
            task_s3_key=pointer,
        )
        session.add_all([task, ExperimentModel(id=task_id, name=task_id)])
        await session.flush()
        # A historical version may exist without being the task's current
        # source. Neither it nor the trial's null version link may be rewritten.
        session.add(
            TaskVersionModel(
                id=task_id + "-v7",
                task_id=task_id,
                version=7,
                task_path="historical",
            )
        )
        session.add(
            TrialModel(
                id=task_id + "-trial",
                name=task_id + "-trial",
                task_id=task_id,
                experiment_id=task_id,
                agent="codex",
                provider="openai",
                model="test",
                queue_key="test",
            )
        )
        await session.commit()
    try:
        async with get_session() as session:
            source = await resolve_task_file_source(
                session, task_id=task_id, version=None
            )
            assert source.version is None
            index = await session.get(FileIndexModel, source.index_key)
            assert index.task_id == task_id
            assert index.task_version_id is None
            # Only this test's job should be selected, even when other tests
            # have left unrelated durable work pending in this database.
            index.next_attempt_at = datetime(1970, 1, 1, tzinfo=UTC)
            await session.commit()
        with pytest.raises(HTTPException) as pending:
            await read_file_index(source_key=source.index_key)
        assert pending.value.status_code == 503
        assert await backfill_file_indexes(limit=1) == 1
        monkeypatch.setattr(
            helpers,
            "get_storage_client",
            lambda: pytest.fail("GET must not scan storage"),
        )
        result = await helpers.list_task_files_s3(
            task_id=task_id,
            task_s3_prefix=pointer,
            version=None,
            expanded_manifest_key=None,
            index_key=source.index_key,
            prefix=None,
            cursor=None,
            recursive=False,
            inline=False,
            presign=False,
            indexed=True,
            limit=100,
        )
        assert [(f["path"], f["size"]) for f in result["files"]] == [
            ("instruction.md", len(content))
        ]
        assert result["files"][0]["key"] == (
            archive_key + "#instruction.md"
            if layout == "archive"
            else root + "instruction.md"
        )
        if layout == "archive":
            storage.list_objects_all.assert_not_awaited()
        else:
            storage.list_objects_all.assert_awaited_once_with(root)
        async with get_session() as session:
            task = await session.get(TaskModel, task_id)
            trial = await session.get(TrialModel, task_id + "-trial")
            assert task.current_version_id is None
            assert task.task_s3_key == pointer
            assert trial.task_version_id is None
            assert (
                await session.scalars(
                    select(TaskVersionModel.version).where(
                        TaskVersionModel.task_id == task_id
                    )
                )
            ).all() == [7]
            task.task_s3_key = root + "replacement/"
            await session.commit()
        async with get_session() as session:
            replacement = await resolve_task_file_source(
                session, task_id=task_id, version=None
            )
            assert replacement.index_key != source.index_key
            assert (
                await session.get(FileIndexModel, replacement.index_key)
            ).revision is None
        assert (await read_file_index(source_key=source.index_key))["files"][0][
            "path"
        ] == "instruction.md"
    finally:
        async with get_session() as session:
            await session.execute(
                text("DELETE FROM trials WHERE task_id=:id"), {"id": task_id}
            )
            await session.execute(
                text("DELETE FROM task_versions WHERE task_id=:id"), {"id": task_id}
            )
            await session.execute(
                text("DELETE FROM tasks WHERE id=:id"), {"id": task_id}
            )
            await session.execute(
                text("DELETE FROM experiments WHERE id=:id"), {"id": task_id}
            )
            await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("column_exists", [False, True])
async def test_legacy_migration_backfills_existing_sources_and_preserves_links(
    session, column_exists
):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    spec = importlib.util.spec_from_file_location(
        "legacy_file_index_migration",
        Path(__file__).parents[1] / "alembic/versions/legacy_file_index_001.py",
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    connection = await session.connection()
    # Isolate the migration from other tests' domain tables. The surrounding
    # test transaction rolls back this entire schema, including its triggers.
    schema = "legacy_migration_" + uuid4().hex
    await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
    await connection.execute(
        text("""
        CREATE TABLE tasks (
            id varchar(128) PRIMARY KEY, current_version_id text,
            task_s3_key text, deleted_at timestamptz
        )
    """)
    )
    await connection.execute(
        text("""
        CREATE TABLE file_indexes (
            source_key text PRIMARY KEY, task_version_id text, revision text
        )
    """)
    )
    if column_exists:
        await connection.execute(
            text(
                "ALTER TABLE file_indexes ADD COLUMN task_id varchar(128) REFERENCES tasks(id) ON DELETE CASCADE"
            )
        )
    await connection.execute(
        text("CREATE TABLE trials (id text PRIMARY KEY, task_version_id text)")
    )
    await connection.execute(
        text("""
        INSERT INTO tasks VALUES
            ('legacy', NULL, 'imports/original/', NULL),
            ('canonical', NULL, NULL, NULL),
            ('versioned', 'version-7', 'imports/versioned/', NULL),
            ('deleted', NULL, 'imports/deleted/', now())
    """)
    )
    await connection.execute(
        text(
            "INSERT INTO trials VALUES ('legacy-trial', NULL), ('historical-trial', 'version-7')"
        )
    )

    def upgrade(sync_connection):
        with Operations.context(MigrationContext.configure(sync_connection)):
            migration.upgrade()

    await connection.run_sync(upgrade)
    queued = (
        await connection.execute(
            text("SELECT source_key, task_id FROM file_indexes ORDER BY task_id")
        )
    ).all()
    assert queued == [
        ("task:canonical:", "canonical"),
        ("task:legacy:imports/original/", "legacy"),
    ]
    assert (
        await connection.execute(
            text("SELECT current_version_id FROM tasks WHERE id='versioned'")
        )
    ).scalar_one() == "version-7"
    assert (
        await connection.execute(
            text("SELECT id, task_version_id FROM trials ORDER BY id")
        )
    ).all() == [("historical-trial", "version-7"), ("legacy-trial", None)]
    await connection.execute(
        text("UPDATE tasks SET task_s3_key='imports/replaced/' WHERE id='legacy'")
    )
    assert (
        await connection.execute(
            text("SELECT count(*) FROM file_indexes WHERE task_id='legacy'")
        )
    ).scalar_one() == 2
    await connection.execute(text("DELETE FROM tasks WHERE id='legacy'"))
    assert (
        await connection.execute(
            text("SELECT count(*) FROM file_indexes WHERE task_id='legacy'")
        )
    ).scalar_one() == 0


@pytest.mark.asyncio
async def test_oversize_archive_is_indexed_without_extraction_or_repeated_jobs(
    monkeypatch,
):
    import io
    import tarfile
    from pathlib import Path
    from datetime import datetime, UTC
    import oddish.db
    import oddish.queue
    from oddish.config import settings
    from oddish.core.file_index import backfill_file_indexes, index_task_archive
    from oddish.core.task_files import resolve_task_file_source
    from oddish.db import TaskModel, TaskVersionModel
    from oddish.db.storage import StorageClient
    from oddish.workers.queue import task_expand_handler

    task_id = "archive-index-" + uuid4().hex[:16]
    version_id = task_id + "-v1"
    root = f"tasks/{task_id}/v1/"
    archive_key = root + ".oddish-task.tar.gz"
    content = b"Read this archive member\n"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in ["instruction.md", "nested/file.txt"]:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    archive_bytes = buffer.getvalue()
    storage = StorageClient()
    storage.head_object = AsyncMock(
        return_value={"ContentLength": len(archive_bytes), "ETag": task_id}
    )
    storage.download_bytes = AsyncMock(return_value=archive_bytes)
    downloads = []

    async def download_file(key, path):
        assert key == archive_key
        downloads.append(path)
        Path(path).write_bytes(archive_bytes)

    storage.download_file = download_file
    storage.upload_bytes = AsyncMock(
        side_effect=AssertionError("indexing must not extract/upload members")
    )
    monkeypatch.setattr(oddish.db, "get_storage_client", lambda: storage)
    monkeypatch.setattr(task_expand_handler, "get_storage_client", lambda: storage)
    monkeypatch.setattr(settings, "tasks_expand_max_bytes", 1)
    enqueue = AsyncMock()
    monkeypatch.setattr(oddish.queue, "enqueue_task_expand_worker_job", enqueue)
    async with get_session() as session:
        session.add(
            TaskModel(id=task_id, name=task_id, user="tester", task_path="archive")
        )
        await session.flush()
        session.add(
            TaskVersionModel(
                id=version_id,
                task_id=task_id,
                version=1,
                task_path="archive",
                task_s3_key=root,
                content_hash="before",
            )
        )
        await session.flush()
        task = await session.get(TaskModel, task_id)
        task.current_version_id = version_id
        index = await session.get(FileIndexModel, f"expand:{version_id}")
        index.next_attempt_at = datetime(1970, 1, 1, tzinfo=UTC)
    try:
        assert await backfill_file_indexes(limit=1) == 0
        enqueue.assert_awaited_once()
        result = await task_expand_handler.run_task_expand_job(task_id, 1)
        assert result["reason"] == "archive_too_large"
        assert result["directory_indexed"] is True
        assert len(downloads) == 1 and not downloads[0].exists()
        storage.download_bytes.assert_not_awaited()  # no in-memory archive cache
        storage.upload_bytes.assert_not_awaited()
        async with get_session() as session:
            source = await resolve_task_file_source(session, task_id=task_id, version=1)
            version = await session.get(TaskVersionModel, version_id)
            assert version.expanded_at is None and version.expanded_manifest_key is None
        listing = await read_file_index(
            source_key=source.index_key, directories=["", "nested"]
        )
        assert (
            listing["directories"][""]["files"][0]["key"]
            == archive_key + "#instruction.md"
        )
        assert listing["directories"]["nested"]["files"][0]["size"] == len(content)
        body = await storage.get_task_file_content(
            task_id=task_id,
            version=1,
            task_s3_prefix=root,
            expanded=False,
            file_path="instruction.md",
            presign=False,
        )
        assert body["content"] == content.decode()
        # A ready index is no longer durable pending work, even after retry time.
        async with get_session() as session:
            index = await session.get(FileIndexModel, source.index_key)
            index.next_attempt_at = datetime(1970, 1, 1, tzinfo=UTC)
        enqueue.reset_mock()
        await backfill_file_indexes()
        assert not any(
            call.kwargs["task_id"] == task_id for call in enqueue.await_args_list
        )
        # An in-place upload must hide the old directory and reject a late scan.
        async with get_session() as session:
            version = await session.get(TaskVersionModel, version_id)
            version.content_hash = "after"
        with pytest.raises(HTTPException) as pending:
            await read_file_index(source_key=source.index_key)
        assert pending.value.status_code == 503
        assert not await index_task_archive(
            storage,
            task_id=task_id,
            version=1,
            archive_key=archive_key,
            expected_content_hash="before",
        )
        assert await index_task_archive(
            storage,
            task_id=task_id,
            version=1,
            archive_key=archive_key,
            expected_content_hash="after",
        )
        assert (await read_file_index(source_key=source.index_key))["files"]
    finally:
        async with get_session() as session:
            await session.execute(delete(TaskModel).where(TaskModel.id == task_id))

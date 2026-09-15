"""Prepared directory metadata; authorized callers select the source key first."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
import time
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import delete, select, union_all, true
from sqlalchemy.dialects.postgresql import insert

from oddish.db.models import FileIndexModel, FileEntryModel

logger = logging.getLogger(__name__)


def directory_entries(files: list[dict]) -> list[dict]:
    """Materialize parents once at publication, including empty intermediate paths."""
    entries = {}
    for file in files:
        path = file["path"]
        if file.get("skipped") or not path:
            continue
        parts = path.split("/")
        for length in range(1, len(parts)):
            directory = "/".join(parts[:length])
            entries[directory] = dict(
                path=directory,
                parent="/".join(parts[: length - 1]),
                size=None,
                is_directory=True,
                artifact=False,
            )
        entries[path] = dict(
            path=path,
            parent="/".join(parts[:-1]),
            size=file.get("size"),
            is_directory=False,
            artifact="artifacts" in parts[:-1],
        )
    return list(entries.values())


async def publish_file_index(
    session,
    *,
    source_key: str,
    root_prefix: str,
    files: list[dict],
    only_if_pending: bool = False,
) -> None:
    """Publish only after uploads succeed; replace inventory in one transaction."""
    from oddish.db import utcnow

    await session.execute(
        insert(FileIndexModel)
        .values(source_key=source_key, root_prefix=root_prefix)
        .on_conflict_do_nothing()
    )
    current_revision = await session.scalar(
        select(FileIndexModel.revision)
        .where(FileIndexModel.source_key == source_key)
        .with_for_update()
    )
    # A background scan may have started before the writer published all files.
    if only_if_pending and current_revision is not None:
        return
    await session.execute(
        delete(FileEntryModel).where(FileEntryModel.source_key == source_key)
    )
    entries = directory_entries(files)
    for offset in range(0, len(entries), 500):
        # Bind NULL directory sizes explicitly in one multi-row INSERT. ORM
        # bulk execution otherwise splits alternating directory/file shapes
        # into individual statements because it omits None-valued columns.
        await session.execute(
            insert(FileEntryModel).values(
                [
                    dict(source_key=source_key, **entry)
                    for entry in entries[offset : offset + 500]
                ]
            )
        )
    from sqlalchemy import update

    await session.execute(
        update(FileIndexModel)
        .where(FileIndexModel.source_key == source_key)
        .values(root_prefix=root_prefix, revision=uuid4().hex, refreshed_at=utcnow())
    )


async def read_file_index(
    *,
    source_key: str | None,
    directories: list[str] | None = None,
    prefix: str | None = None,
    cursor: str | None = None,
    limit: int = 100,
    artifacts: bool = False,
    revision: str | None = None,
) -> dict:
    """One metadata query per bounded page, no storage discovery or body reads."""
    from oddish.db import get_read_session
    from oddish.db.storage import normalize_s3_relative_path

    if (
        not 1 <= limit <= 1000
        or directories is not None
        and not 1 <= len(directories) <= 8
    ):
        raise HTTPException(400, "Request 1–8 directories and 1–1000 entries per page")
    if directories is not None and (prefix or cursor or artifacts):
        raise HTTPException(
            400, "Directory batches cannot combine prefix, cursor, or artifacts"
        )
    paths = list(
        dict.fromkeys(
            normalize_s3_relative_path(path).rstrip("/")
            for path in (directories or [prefix or ""])
        )
    )
    async with get_read_session() as session:
        page_queries = []
        from sqlalchemy import literal

        for path in paths:
            query = select(
                FileEntryModel.path,
                FileEntryModel.size,
                FileEntryModel.is_directory,
                literal(path).label("directory"),
            ).where(FileEntryModel.source_key == source_key)
            query = (
                query.where(FileEntryModel.artifact.is_(True))
                if artifacts
                else query.where(FileEntryModel.parent == path)
            )
            if cursor:
                query = query.where(
                    FileEntryModel.path > normalize_s3_relative_path(cursor)
                )
            page_queries.append(query.order_by(FileEntryModel.path).limit(limit + 1))
        page_rows = union_all(*page_queries).lateral()
        records = (
            (
                await session.execute(
                    select(
                        FileIndexModel.root_prefix,
                        FileIndexModel.revision,
                        page_rows.c.path,
                        page_rows.c.size,
                        page_rows.c.is_directory,
                        page_rows.c.directory,
                    )
                    .select_from(FileIndexModel)
                    .outerjoin(page_rows, true())
                    .where(FileIndexModel.source_key == source_key)
                )
            )
            .mappings()
            .all()
        )
        if not records:
            raise HTTPException(404, "No file directory is available for this source")
        if records[0]["revision"] is None:
            raise HTTPException(
                503, "File directory is being prepared", headers={"Retry-After": "2"}
            )
        root_prefix, current_revision = (
            records[0]["root_prefix"],
            records[0]["revision"],
        )
        if revision and revision != current_revision:
            raise HTTPException(409, "File directory changed; reload its contents")
        pages = {}
        for path in paths:
            rows = [
                row
                for row in records
                if row["directory"] == path and row["path"] is not None
            ]
            rows.sort(key=lambda row: row["path"])
            shown = rows[:limit]
            pages[path] = dict(
                files=[
                    dict(
                        path=row["path"],
                        key=root_prefix + row["path"],
                        size=row["size"],
                    )
                    for row in shown
                    if not row["is_directory"]
                ],
                dirs=[dict(path=row["path"]) for row in shown if row["is_directory"]],
                cursor=shown[-1]["path"] if len(rows) > limit else None,
                truncated=len(rows) > limit,
                recursive=False,
                presigned=False,
                prefix=root_prefix + path,
                source_hash=current_revision,
            )
        return (
            dict(directories=pages, source_hash=current_revision)
            if directories is not None
            else pages[paths[0]]
        )


async def index_trial_upload(
    storage,
    *,
    trial,
    files: list[dict] | None = None,
    only_if_pending: bool = False,
) -> None:
    """Resolve Harbor's authoritative child once, before publishing its directory."""
    from oddish.core.trial_artifacts import (
        resolve_trial_artifact_layout,
        TrialArtifactMode,
    )
    from oddish.db import get_session

    layout = await resolve_trial_artifact_layout(trial, storage)
    if layout.mode is TrialArtifactMode.UNAVAILABLE:
        raise ValueError(layout.failure_reason)
    root = layout.artifact_prefix
    assert root is not None
    if files is None:
        objects = await storage.list_objects_all(root)
        files = [
            dict(path=obj["key"][len(root) :], size=obj.get("size")) for obj in objects
        ]
    else:
        child = root[len(layout.attempt_prefix) :]
        files = [
            dict(path=f["path"][len(child) :], size=f.get("size"))
            for f in files
            if f["path"].startswith(child)
        ]
    async with get_session() as session:
        await publish_file_index(
            session,
            source_key=trial_index_key(trial),
            root_prefix=root,
            files=files,
            only_if_pending=only_if_pending,
        )
        await session.commit()


def trial_index_key(trial, attempt: int | None = None) -> str:
    if attempt is not None and attempt != (trial.attempts or 0):
        raise HTTPException(409, "Trial attempt changed; reload its directory")
    return f"trial:{trial.id}:{trial.attempts or 0}:{trial.trial_s3_key or ''}"


async def index_task_archive(
    storage,
    *,
    task_id: str,
    version: int,
    archive_key: str,
    expected_content_hash: str | None,
) -> bool:
    """Publish metadata from the expansion worker without extracting objects.

    The existing worker owns heartbeats and retries for this potentially long
    scan. Lock the version only after storage work, checking for an overwrite
    before publishing so an old archive cannot replace the new directory.
    """
    from oddish.db import get_session, TaskVersionModel
    from oddish.db.storage import StorageClient

    files = await storage.list_task_archive_members(archive_key)
    async with get_session() as session:
        row = await session.scalar(
            select(TaskVersionModel)
            .where(
                TaskVersionModel.task_id == task_id, TaskVersionModel.version == version
            )
            .with_for_update()
        )
        if (
            row is None
            or row.deleted_at is not None
            or row.content_hash != expected_content_hash
            or row.expanded_manifest_key is not None
            or row.task_s3_key
            and StorageClient._task_archive_key_from_prefix(row.task_s3_key)
            != archive_key
        ):
            return False
        await publish_file_index(
            session,
            source_key=f"expand:{row.id}",
            root_prefix=archive_key + "#",
            files=files,
            only_if_pending=True,
        )
    return True


async def backfill_file_indexes(*, limit: int = 8) -> int:
    """Drain durable index jobs; a crash leaves the pending row available."""
    from oddish.db import (
        get_read_session,
        get_session,
        get_storage_client,
        TaskVersionModel,
        TrialModel,
        TaskModel,
        utcnow,
    )
    from sqlalchemy import text
    from oddish.core.task_files import legacy_task_index_key

    storage = get_storage_client()
    completed = 0
    started = time.monotonic()
    async with get_session() as lock_session:
        if not await lock_session.scalar(
            text("SELECT pg_try_advisory_xact_lock(716490242)")
        ):
            return 0
        async with get_read_session() as session:
            pending = (
                await session.execute(
                    select(
                        FileIndexModel.source_key,
                        FileIndexModel.task_version_id,
                        FileIndexModel.trial_id,
                        FileIndexModel.task_id,
                    )
                    .where(
                        FileIndexModel.revision.is_(None),
                        FileIndexModel.next_attempt_at <= utcnow(),
                    )
                    .order_by(FileIndexModel.next_attempt_at)
                    .limit(limit)
                )
            ).all()
        for key, version_id, trial_id, task_id in pending:
            if time.monotonic() - started > 30:
                break
            try:
                async with asyncio.timeout(10):
                    async with get_read_session() as session:
                        version = (
                            await session.get(TaskVersionModel, version_id)
                            if version_id
                            else None
                        )
                        trial = (
                            await session.get(TrialModel, trial_id)
                            if trial_id
                            else None
                        )
                        task = (
                            await session.get(TaskModel, task_id) if task_id else None
                        )
                    if version is not None:
                        if version.expanded_manifest_key is None:
                            from oddish.queue import enqueue_task_expand_worker_job

                            async with get_session() as session:
                                org_id = await session.scalar(
                                    select(TaskModel.org_id).where(
                                        TaskModel.id == version.task_id
                                    )
                                )
                                await enqueue_task_expand_worker_job(
                                    session,
                                    task_id=version.task_id,
                                    version=version.version,
                                    org_id=org_id,
                                )
                                await session.commit()
                            await postpone_file_index(key)
                            continue
                        if key != version.expanded_manifest_key:
                            # A newer source superseded this pending job. The trigger
                            # already enqueued the new source; retain published old indexes.
                            async with get_session() as session:
                                await session.execute(
                                    delete(FileIndexModel).where(
                                        FileIndexModel.source_key == key,
                                        FileIndexModel.revision.is_(None),
                                    )
                                )
                                await session.commit()
                            continue
                        manifest = await storage.download_json(key)
                        async with get_session() as session:
                            await publish_file_index(
                                session,
                                source_key=key,
                                root_prefix=key.rsplit("/", 1)[0] + "/",
                                files=manifest["files"],
                                only_if_pending=True,
                            )
                            await session.commit()
                    elif trial is not None and key == trial_index_key(trial):
                        await index_trial_upload(
                            storage, trial=trial, only_if_pending=True
                        )
                    elif (
                        task is not None
                        and task.current_version_id is None
                        and task.deleted_at is None
                        and key == legacy_task_index_key(task.id, task.task_s3_key)
                    ):
                        listing = await storage.list_task_files(
                            task_id=task.id,
                            task_s3_prefix=task.task_s3_key,
                            version=None,
                            prefix=None,
                            cursor=None,
                            recursive=True,
                            limit=1000,
                            presign=False,
                            inline=False,
                        )
                        # Archive members have virtual keys; their content still
                        # goes through the existing authorized archive reader.
                        root = (
                            listing["archive_key"] + "#"
                            if listing.get("archive_key")
                            else listing["prefix"]
                        )
                        async with get_session() as session:
                            await publish_file_index(
                                session,
                                source_key=key,
                                root_prefix=root,
                                files=listing["files"],
                                only_if_pending=True,
                            )
                            await session.commit()
                    else:
                        async with get_session() as session:
                            await session.execute(
                                delete(FileIndexModel).where(
                                    FileIndexModel.source_key == key,
                                    FileIndexModel.revision.is_(None),
                                )
                            )
                            await session.commit()
                    completed += 1
            except Exception:
                logger.exception("file index preparation failed source_key=%s", key)
                await postpone_file_index(key)
    return completed


async def postpone_file_index(source_key: str) -> None:
    from oddish.db import get_session, utcnow

    async with get_session() as session:
        await session.execute(
            insert(FileIndexModel)
            .values(
                source_key=source_key, next_attempt_at=utcnow() + timedelta(minutes=5)
            )
            .on_conflict_do_update(
                index_elements=[FileIndexModel.source_key],
                set_={"next_attempt_at": utcnow() + timedelta(minutes=5)},
            )
        )
        await session.commit()


async def run_file_index_maintenance() -> None:
    while True:
        try:
            await backfill_file_indexes()
        except Exception:
            logger.exception("file directory maintenance failed")
        await asyncio.sleep(5)

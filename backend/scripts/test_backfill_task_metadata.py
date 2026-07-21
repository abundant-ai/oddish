from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

import backfill_task_metadata as job
from oddish.core.endpoints import browse_tasks_core
from oddish.db import get_session
from oddish.db.models import (
    MetadataSource,
    TagAssignmentModel,
    TagAssignmentState,
    TagModel,
    TaskModel,
    TaskUploadEventModel,
    TaskVersionModel,
)

_VALID_TOML = """\
version = "1.0"

[metadata]
category = "ml-training"
description = "Backfilled task."
author_name = "Ada"
author_organization = "Abundant AI"

[environment]
cpus = 2
memory_mb = 4096
allow_internet = false

[agent]
timeout_sec = 300

[verifier]
timeout_sec = 60
"""

_MALFORMED_TOML = "this [ is not : valid toml"


class _FakeStorage:
    """Duck-typed StorageClient stub -- no real S3 required."""

    def __init__(self, objects: dict[str, bytes] | None = None):
        self.objects = objects or {}

    async def object_exists(self, key: str) -> bool:
        return key in self.objects

    async def download_bytes(self, key: str) -> bytes:
        return self.objects[key]


def _org() -> str:
    return f"taskreg-backfill-{uuid.uuid4().hex[:8]}"


async def _make_task_with_version(
    session,
    *,
    org_id: str,
    name: str,
    metadata_source: MetadataSource | None = None,
    category: str | None = None,
    tags: dict | None = None,
) -> tuple[TaskModel, TaskVersionModel]:
    task = TaskModel(
        name=name,
        org_id=org_id,
        user="tester",
        task_path=f"s3://tasks/{name}",
        metadata_source=metadata_source,
        category=category,
        tags=tags or {},
    )
    session.add(task)
    await session.flush()
    version = TaskVersionModel(
        id=f"{task.id}-v1", task_id=task.id, version=1, task_path=task.task_path
    )
    session.add(version)
    await session.flush()
    task.current_version_id = version.id
    await session.flush()
    return task, version


def _storage_for(task_id: str, version: int, toml_text: str | None) -> _FakeStorage:
    prefix = job._expanded_prefix_for(task_id, version)
    objects: dict[str, bytes] = {f"{prefix}.oddish-manifest.json": b"{}"}
    if toml_text is not None:
        objects[f"{prefix}task.toml"] = toml_text.encode("utf-8")
    return _FakeStorage(objects)


async def _active_category_tags(task_id: str) -> list[TagAssignmentModel]:
    async with get_session() as session:
        return list(
            (
                await session.execute(
                    select(TagAssignmentModel)
                    .join(TagModel, TagModel.id == TagAssignmentModel.tag_id)
                    .where(
                        TagAssignmentModel.task_id == task_id,
                        TagAssignmentModel.state == TagAssignmentState.ACTIVE,
                        TagModel.key == "category",
                    )
                )
            )
            .scalars()
            .all()
        )


async def _purge(org_id: str) -> None:
    async with get_session() as session:
        await session.execute(
            TagAssignmentModel.__table__.delete().where(
                TagAssignmentModel.org_id == org_id
            )
        )
        await session.execute(
            TagModel.__table__.delete().where(TagModel.org_id == org_id)
        )
        # Cascades to task_versions and task_upload_events (FK ondelete=CASCADE).
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.org_id == org_id)
        )


@pytest.mark.asyncio
async def test_dry_run_makes_no_database_changes():
    org_id = _org()
    try:
        async with get_session() as session:
            task, _version = await _make_task_with_version(
                session, org_id=org_id, name=f"dryrun-{org_id}"
            )
            task_id = task.id
        storage = _storage_for(task_id, 1, _VALID_TOML)

        summary = await job.run_backfill(
            task_names=[task_id], execute=False, force=False, storage=storage
        )
        assert summary.backfilled == [task_id]

        async with get_session() as session:
            refreshed = await session.get(TaskModel, task_id)
            assert refreshed.category is None
            assert refreshed.metadata_source is None
            refreshed_version = await session.get(
                TaskVersionModel, refreshed.current_version_id
            )
            assert refreshed_version.cpus is None
            assert refreshed_version.category_snapshot is None

        assert (await _active_category_tags(task_id)) == []
    finally:
        await _purge(org_id)


@pytest.mark.asyncio
async def test_execute_populates_columns_and_stamps_backfill_source():
    org_id = _org()
    try:
        async with get_session() as session:
            task, _version = await _make_task_with_version(
                session, org_id=org_id, name=f"exec-{org_id}"
            )
            task_id = task.id
        storage = _storage_for(task_id, 1, _VALID_TOML)

        summary = await job.run_backfill(
            task_names=[task_id], execute=True, force=False, storage=storage
        )
        assert summary.backfilled == [task_id]

        async with get_session() as session:
            refreshed = await session.get(TaskModel, task_id)
            assert refreshed.category == "ml-training"
            assert refreshed.description == "Backfilled task."
            assert refreshed.author_name == "Ada"
            assert refreshed.author_organization == "Abundant AI"
            assert refreshed.metadata_source == MetadataSource.BACKFILL
            assert refreshed.metadata_updated_at is not None

            refreshed_version = await session.get(
                TaskVersionModel, refreshed.current_version_id
            )
            assert refreshed_version.cpus == 2
            assert refreshed_version.memory_mb == 4096
            # NOT asserting allow_internet: harbor's TaskConfig clears this
            # deprecated field to None after mapping it into network_mode
            # (config.py's handle_deprecated_environment_allow_internet), so
            # project_task_config's environment.get("allow_internet") is None
            # for every task.toml now -- identically for fresh uploads and
            # backfills, since both go through the same pure function.
            assert refreshed_version.category_snapshot == "ml-training"
            assert refreshed_version.description_snapshot == "Backfilled task."
    finally:
        await _purge(org_id)


@pytest.mark.asyncio
async def test_derived_category_tag_findable_via_browse_filter():
    org_id = _org()
    try:
        async with get_session() as session:
            task, _version = await _make_task_with_version(
                session, org_id=org_id, name=f"findme-{org_id}"
            )
            task_id = task.id
        storage = _storage_for(task_id, 1, _VALID_TOML)

        summary = await job.run_backfill(
            task_names=[task_id], execute=True, force=False, storage=storage
        )
        assert summary.backfilled == [task_id]
        assert len(await _active_category_tags(task_id)) == 1

        async with get_session() as session:
            resp = await browse_tasks_core(
                session, org_id=org_id, tags_all=["category:ml-training"]
            )
            ids = {item.id for item in resp.items}
            assert task_id in ids
    finally:
        await _purge(org_id)


@pytest.mark.asyncio
async def test_client_metadata_source_is_skipped_not_overwritten():
    org_id = _org()
    try:
        async with get_session() as session:
            task, _version = await _make_task_with_version(
                session,
                org_id=org_id,
                name=f"client-{org_id}",
                metadata_source=MetadataSource.CLIENT,
                category="human-set",
            )
            task_id = task.id
        storage = _storage_for(task_id, 1, _VALID_TOML)

        summary = await job.run_backfill(
            task_names=[task_id], execute=True, force=False, storage=storage
        )
        assert summary.backfilled == []
        assert any(tid == task_id for tid, _ in summary.skipped)

        async with get_session() as session:
            refreshed = await session.get(TaskModel, task_id)
            assert refreshed.category == "human-set"
            assert refreshed.metadata_source == MetadataSource.CLIENT
    finally:
        await _purge(org_id)


@pytest.mark.asyncio
async def test_malformed_toml_skipped_and_run_continues():
    org_id = _org()
    try:
        async with get_session() as session:
            bad_task, _bv = await _make_task_with_version(
                session, org_id=org_id, name=f"bad-{org_id}"
            )
            good_task, _gv = await _make_task_with_version(
                session, org_id=org_id, name=f"good-{org_id}"
            )
            bad_id, good_id = bad_task.id, good_task.id

        objects: dict[str, bytes] = {}
        objects.update(_storage_for(bad_id, 1, _MALFORMED_TOML).objects)
        objects.update(_storage_for(good_id, 1, _VALID_TOML).objects)
        storage = _FakeStorage(objects)

        summary = await job.run_backfill(
            task_names=[bad_id, good_id], execute=True, force=False, storage=storage
        )
        assert good_id in summary.backfilled
        skipped_ids = {tid for tid, _ in summary.skipped}
        assert bad_id in skipped_ids
        reason = next(r for tid, r in summary.skipped if tid == bad_id)
        assert "malformed" in reason.lower()

        async with get_session() as session:
            refreshed_bad = await session.get(TaskModel, bad_id)
            assert refreshed_bad.metadata_source is None
            refreshed_good = await session.get(TaskModel, good_id)
            assert refreshed_good.metadata_source == MetadataSource.BACKFILL
    finally:
        await _purge(org_id)


@pytest.mark.asyncio
async def test_running_twice_is_idempotent():
    org_id = _org()
    try:
        async with get_session() as session:
            task, _version = await _make_task_with_version(
                session, org_id=org_id, name=f"twice-{org_id}"
            )
            task_id = task.id
        storage = _storage_for(task_id, 1, _VALID_TOML)

        first = await job.run_backfill(
            task_names=[task_id], execute=True, force=False, storage=storage
        )
        assert first.backfilled == [task_id]
        # Not CLIENT-sourced, so a second run refreshes rather than skipping.
        second = await job.run_backfill(
            task_names=[task_id], execute=True, force=False, storage=storage
        )
        assert second.backfilled == [task_id]

        async with get_session() as session:
            refreshed = await session.get(TaskModel, task_id)
            assert refreshed.category == "ml-training"
            assert refreshed.metadata_source == MetadataSource.BACKFILL

        # rebuild_derived_tags replaces rather than accumulates.
        assert len(await _active_category_tags(task_id)) == 1
    finally:
        await _purge(org_id)


@pytest.mark.asyncio
async def test_recovers_ci_pr_number_and_dedupes_on_rerun():
    org_id = _org()
    try:
        github_meta = json.dumps({"pr_number": "412", "pr_repo": "abundant-ai/oddish"})
        async with get_session() as session:
            task, _version = await _make_task_with_version(
                session,
                org_id=org_id,
                name=f"ghmeta-{org_id}",
                tags={"github_meta": github_meta},
            )
            task_id = task.id
        storage = _storage_for(task_id, 1, _VALID_TOML)

        await job.run_backfill(
            task_names=[task_id], execute=True, force=False, storage=storage
        )
        await job.run_backfill(
            task_names=[task_id], execute=True, force=False, storage=storage
        )

        async with get_session() as session:
            events = (
                (
                    await session.execute(
                        select(TaskUploadEventModel).where(
                            TaskUploadEventModel.task_id == task_id,
                            TaskUploadEventModel.uploader_cli_version
                            == job.BACKFILL_UPLOADER_MARKER,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(events) == 1
            assert events[0].ci_pr_number == 412
            assert events[0].created_version is False
    finally:
        await _purge(org_id)

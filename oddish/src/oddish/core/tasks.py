from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import settings
from oddish.core.tags.enqueue import enqueue_tag_project_worker_job
from oddish.db import Priority, TaskModel, TaskVersionModel, get_session
from oddish.db.models import MetadataSource, TaskUploadEventModel, utcnow
from oddish.db.storage import StorageClient, get_storage_client
from oddish.schemas import (
    TaskMetadata,
    TaskProvenance,
    TaskUploadInitResponse,
    UploadResponse,
)


async def _next_version_number(session: AsyncSession, task_id: str) -> int:
    """Return the next version number for a task (1-indexed)."""
    max_version = await session.scalar(
        select(func.max(TaskVersionModel.version)).where(
            TaskVersionModel.task_id == task_id
        )
    )
    return (max_version or 0) + 1


async def _find_task_by_name(
    session: AsyncSession, task_name: str, org_id: str | None
) -> TaskModel | None:
    """Look up an existing task by ``(org_id, name)``."""
    if org_id is None:
        clause = and_(TaskModel.name == task_name, TaskModel.org_id.is_(None))
    else:
        clause = and_(TaskModel.name == task_name, TaskModel.org_id == org_id)

    return await session.scalar(select(TaskModel).where(clause))


async def _latest_version(
    session: AsyncSession, task_id: str
) -> TaskVersionModel | None:
    """Return the highest-numbered version row for *task_id*, or ``None``."""
    return await session.scalar(
        select(TaskVersionModel)
        .where(TaskVersionModel.task_id == task_id)
        .order_by(TaskVersionModel.version.desc())
        .limit(1)
    )


def _normalize_task_name(name: str) -> str:
    """Normalize a filename or path-like task name into the stored task name."""
    normalized = Path(name).name or name
    stem = Path(normalized).stem
    if stem.endswith(".tar"):
        stem = Path(stem).stem
    return stem or normalized


async def _enqueue_tag_project_for_new_version(
    session, *, task_id: str, version_id: str, org_id: str | None
) -> None:
    """Hook called from ``complete_task_upload`` whenever a new
    ``task_versions`` row is inserted so the new version inherits living
    EXPERIMENT and TASK tags into its ``effective_tag_ids`` array."""
    await enqueue_tag_project_worker_job(
        session,
        scope="VERSION",
        target_id=version_id,
        task_id=task_id,
        org_id=org_id,
        mode="direct",
    )


def _apply_descriptive_metadata(task: TaskModel, metadata: TaskMetadata) -> None:
    """Write current descriptive values onto the task (last-write-wins).

    task.toml is authoritative for these fields; human curation lives in
    DIRECT tag assignments, which this never touches.
    """
    task.description = metadata.description
    task.category = metadata.category
    task.category_raw = metadata.category_raw
    task.author_name = metadata.author_name
    task.author_email = metadata.author_email
    task.author_organization = metadata.author_organization
    task.expert_time_hours = metadata.expert_time_hours
    task.metadata_source = MetadataSource.CLIENT
    task.metadata_updated_at = utcnow()


def _apply_version_metadata(version: TaskVersionModel, metadata: TaskMetadata) -> None:
    """Write hash-backed runtime fields plus the immutable descriptive snapshot."""
    version.allow_internet = metadata.allow_internet
    version.cpus = metadata.cpus
    version.memory_mb = metadata.memory_mb
    version.storage_mb = metadata.storage_mb
    version.gpus = metadata.gpus
    version.gpu_types = metadata.gpu_types or None
    version.agent_timeout_sec = metadata.agent_timeout_sec
    version.verifier_timeout_sec = metadata.verifier_timeout_sec
    version.build_timeout_sec = metadata.build_timeout_sec

    version.description_snapshot = metadata.description
    version.category_snapshot = metadata.category
    version.topic_tags_snapshot = metadata.topic_tags or None
    version.expert_time_hours_snapshot = metadata.expert_time_hours


def record_upload_event(
    session: AsyncSession,
    *,
    task_id: str,
    task_version_id: str | None,
    created_version: bool,
    content_hash: str | None,
    provenance: TaskProvenance | None,
    uploader_user_id: str | None = None,
) -> None:
    """Append one row per upload attempt, including content-unchanged no-ops.

    ``created_version`` is required rather than derived from ``task_version_id``:
    the FK is ON DELETE SET NULL, so a deleted version would otherwise make a real
    upload indistinguishable from a no-op.
    """
    prov = provenance or TaskProvenance()
    session.add(
        TaskUploadEventModel(
            task_id=task_id,
            task_version_id=task_version_id,
            created_version=created_version,
            content_hash=content_hash,
            source_repo=prov.source_repo,
            source_commit=prov.source_commit,
            source_ref=prov.source_ref,
            source_path=prov.source_path,
            ci_provider=prov.ci_provider,
            ci_run_id=prov.ci_run_id,
            ci_run_url=prov.ci_run_url,
            ci_pr_number=prov.ci_pr_number,
            uploader_is_ci=prov.uploader_is_ci,
            uploader_user_id=uploader_user_id,
            uploader_cli_version=prov.uploader_cli_version,
            uploader_host=prov.uploader_host,
        )
    )


def _task_s3_prefix_for_version(task_id: str, version: int) -> str:
    return f"tasks/{task_id}/v{version}/"


def _task_archive_key_for_version(task_id: str, version: int) -> str:
    return (
        f"{_task_s3_prefix_for_version(task_id, version)}"
        f"{StorageClient._TASK_ARCHIVE_OBJECT_NAME}"
    )


async def initialize_task_upload(
    task_name: str,
    *,
    org_id: str | None = None,
    content_hash: str,
    message: str | None = None,
    force_new_version: bool = False,
    task_metadata: TaskMetadata | None = None,
    provenance: TaskProvenance | None = None,
    uploader_user_id: str | None = None,
) -> TaskUploadInitResponse:
    """Prepare a task upload and return direct-upload details when supported."""
    normalized_name = _normalize_task_name(task_name)

    async with get_session() as session:
        existing_task = await _find_task_by_name(session, normalized_name, org_id)
        latest = (
            await _latest_version(session, existing_task.id)
            if existing_task is not None
            else None
        )

        if (
            not force_new_version
            and latest is not None
            and latest.content_hash
            and latest.content_hash == content_hash
        ):
            if task_metadata is not None:
                _apply_descriptive_metadata(existing_task, task_metadata)
            record_upload_event(
                session,
                task_id=existing_task.id,
                task_version_id=None,
                created_version=False,
                content_hash=content_hash,
                provenance=provenance,
                uploader_user_id=uploader_user_id,
            )
            await session.commit()
            return TaskUploadInitResponse(
                task_id=existing_task.id,
                name=normalized_name,
                s3_key=latest.task_s3_key,
                version=latest.version,
                version_id=latest.id,
                existing_task=True,
                content_unchanged=True,
                content_hash=content_hash,
            )

        if existing_task is not None:
            task_id = existing_task.id
            version = await _next_version_number(session, task_id)
            existing = True
        else:
            task_id = f"{normalized_name}-{str(uuid.uuid4())[:8]}"
            version = 1
            existing = False

    version_id = f"{task_id}-v{version}"
    s3_key = _task_s3_prefix_for_version(task_id, version)

    storage = get_storage_client()
    archive_key = _task_archive_key_for_version(task_id, version)
    try:
        upload_url = await storage.get_presigned_upload_url(
            archive_key,
            expiration=3600,
            content_type="application/gzip",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Failed to prepare S3 upload: {str(exc)}"
        ) from exc

    return TaskUploadInitResponse(
        task_id=task_id,
        name=normalized_name,
        s3_key=s3_key,
        version=version,
        version_id=version_id,
        existing_task=existing,
        content_hash=content_hash,
        upload_url=upload_url,
        upload_method="PUT",
        upload_headers={"Content-Type": "application/gzip"},
        requires_completion=True,
    )


async def complete_task_upload(
    *,
    task_id: str,
    task_name: str,
    version: int,
    content_hash: str,
    message: str | None = None,
    org_id: str | None = None,
    created_by_user_id: str | None = None,
    register: bool = False,
    user: str | None = None,
    priority: Priority | None = None,
    task_metadata: TaskMetadata | None = None,
    provenance: TaskProvenance | None = None,
) -> UploadResponse:
    """Finalize a direct-to-S3 upload after the client has uploaded bytes.

    When ``register`` is True and the task does not yet exist in the DB, a
    new ``TaskModel`` + v1 ``TaskVersionModel`` pair is created so the task
    becomes visible in the UI without any trials attached. The default
    (``register=False``) preserves the legacy behavior used by the sweep
    path, where the task row is created later by ``create_task``.
    """
    normalized_name = _normalize_task_name(task_name)
    s3_key = _task_s3_prefix_for_version(task_id, version)
    archive_key = _task_archive_key_for_version(task_id, version)
    version_id = f"{task_id}-v{version}"
    task_path = f"s3://{s3_key}"

    storage = get_storage_client()
    try:
        archive_exists = await storage.object_exists(archive_key)
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Failed to verify S3 upload: {str(exc)}"
        ) from exc
    if not archive_exists:
        raise HTTPException(
            status_code=400, detail="Uploaded task archive not found in S3"
        )

    async with get_session() as session:
        existing_task = await session.get(TaskModel, task_id)

        if existing_task is None and not register:
            # Legacy behavior: leave creation of the task row to the
            # subsequent /tasks/sweep call.
            return UploadResponse(
                task_id=task_id,
                name=normalized_name,
                s3_key=s3_key,
                version=version,
                version_id=version_id,
                content_hash=content_hash,
            )

        if existing_task is None:
            # Upload-only registration path: create the task row and its
            # v1 version so the task is browsable before any trials run.
            new_task = TaskModel(
                id=task_id,
                name=normalized_name,
                org_id=org_id,
                created_by_user_id=created_by_user_id,
                user=user or created_by_user_id or "unknown",
                priority=priority or Priority.LOW,
                task_path=task_path,
                task_s3_key=s3_key,
            )
            session.add(new_task)
            await session.flush()

            version_row = TaskVersionModel(
                id=version_id,
                task_id=task_id,
                version=version,
                task_path=task_path,
                task_s3_key=s3_key,
                content_hash=content_hash,
                message=message,
                created_by_user_id=created_by_user_id,
            )
            session.add(version_row)
            await session.flush()

            new_task.current_version_id = version_id

            if task_metadata is not None:
                _apply_descriptive_metadata(new_task, task_metadata)
                _apply_version_metadata(version_row, task_metadata)
            record_upload_event(
                session,
                task_id=task_id,
                task_version_id=version_id,
                created_version=True,
                content_hash=content_hash,
                provenance=provenance,
                uploader_user_id=created_by_user_id,
            )

            if settings.tasks_expand_archive:
                # Brand-new tasks need the same expansion kick-off as
                # re-uploads; without it the first drawer open still
                # pays the full tarball download+parse cost.
                from oddish.queue import enqueue_task_expand_worker_job

                await enqueue_task_expand_worker_job(
                    session,
                    task_id=task_id,
                    version=version,
                    org_id=new_task.org_id,
                )

            await _enqueue_tag_project_for_new_version(
                session,
                task_id=task_id,
                version_id=version_id,
                org_id=new_task.org_id,
            )

            await session.commit()

            return UploadResponse(
                task_id=task_id,
                name=normalized_name,
                s3_key=s3_key,
                version=version,
                version_id=version_id,
                existing_task=False,
                content_hash=content_hash,
            )

        if org_id is not None and existing_task.org_id != org_id:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        version_row = await session.get(TaskVersionModel, version_id)
        new_version_created = False
        if version_row is None:
            version_row = TaskVersionModel(
                id=version_id,
                task_id=task_id,
                version=version,
                task_path=task_path,
                task_s3_key=s3_key,
                content_hash=content_hash,
                message=message,
                created_by_user_id=created_by_user_id,
            )
            session.add(version_row)
            new_version_created = True
            # Force the INSERT to land before we point ``tasks.current_version_id``
            # at it. The unit of work otherwise emits the ``tasks`` UPDATE
            # ahead of the ``task_versions`` INSERT during the next implicit
            # flush (e.g. inside ``enqueue_task_expand_worker_job`` below),
            # tripping ``fk_tasks_current_version_id``. Mirrors the
            # explicit-flush pattern the new-task branch above already uses.
            await session.flush()

        existing_task.task_path = task_path
        existing_task.task_s3_key = s3_key
        existing_task.current_version_id = version_id

        if settings.tasks_expand_archive:
            # Kick off the per-file expansion so the task-files drawer
            # can list directly from S3 on the next open. Lazy import to
            # avoid a circular dep via ``oddish.queue`` -> this module.
            from oddish.queue import enqueue_task_expand_worker_job

            await enqueue_task_expand_worker_job(
                session,
                task_id=task_id,
                version=version,
                org_id=existing_task.org_id,
            )

        if new_version_created:
            await _enqueue_tag_project_for_new_version(
                session,
                task_id=task_id,
                version_id=version_id,
                org_id=existing_task.org_id,
            )

        if task_metadata is not None:
            _apply_descriptive_metadata(existing_task, task_metadata)
            if new_version_created:
                _apply_version_metadata(version_row, task_metadata)
        record_upload_event(
            session,
            task_id=task_id,
            task_version_id=version_id,
            created_version=new_version_created,
            content_hash=content_hash,
            provenance=provenance,
            uploader_user_id=created_by_user_id,
        )

        await session.commit()

    return UploadResponse(
        task_id=task_id,
        name=normalized_name,
        s3_key=s3_key,
        version=version,
        version_id=version_id,
        existing_task=True,
        content_hash=content_hash,
    )


async def resolve_task_storage(
    task_id: str,
    *,
    version: int | None = None,
    s3_missing_detail: str | None = None,
    local_missing_detail: str | None = None,
) -> tuple[str, str | None]:
    """Resolve task path from S3, verifying existence.

    When *version* is given the versioned prefix ``tasks/{task_id}/v{version}/``
    is checked first.  Falls back to the legacy un-versioned prefix for
    backwards compatibility with tasks uploaded before versioning.

    When *version* is ``None`` (e.g. first sweep for a newly uploaded task),
    the function checks whether the archive exists at the unversioned root.
    If it only exists under a versioned sub-prefix (the init/complete upload
    path), that versioned prefix is returned so downstream code uses the
    correct S3 key.
    """
    storage = get_storage_client()
    archive_name = StorageClient._TASK_ARCHIVE_OBJECT_NAME

    # Try versioned prefix first
    if version is not None:
        versioned_key = f"tasks/{task_id}/v{version}/"
        try:
            if await storage.prefix_exists(versioned_key):
                return f"s3://{versioned_key}", versioned_key
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to check S3: {str(e)}")

    # Check unversioned root archive
    task_s3_key = f"tasks/{task_id}/"
    root_archive_key = f"{task_s3_key}{archive_name}"
    try:
        if await storage.object_exists(root_archive_key):
            return f"s3://{task_s3_key}", task_s3_key
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to check S3: {str(e)}")

    # The init/complete upload path places archives at versioned sub-prefixes
    # (tasks/{task_id}/v{N}/).  Probe for the latest versioned archive so
    # the caller gets the exact prefix where the tarball lives.
    try:
        all_keys = await storage.list_keys(task_s3_key)
        versioned_archives = sorted(
            (k for k in all_keys if k.endswith(f"/{archive_name}")),
            reverse=True,
        )
        if versioned_archives:
            best = versioned_archives[0]
            prefix = best[: best.rfind("/") + 1]
            return f"s3://{prefix}", prefix
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to check S3: {str(e)}")

    # No archive found — check if the prefix contains anything at all
    try:
        exists = await storage.prefix_exists(task_s3_key)
        if not exists:
            raise HTTPException(
                status_code=404,
                detail=s3_missing_detail
                or local_missing_detail
                or f"Task {task_id} not found in S3",
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to check S3: {str(e)}")

    return f"s3://{task_s3_key}", task_s3_key

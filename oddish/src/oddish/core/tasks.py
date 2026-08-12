from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import settings
from oddish.core.tags.enqueue import enqueue_tag_project_worker_job
from oddish.db import Priority, TaskModel, TaskVersionModel, get_session
from oddish.db.storage import StorageClient, get_storage_client
from oddish.schemas import TaskUploadInitResponse, UploadResponse


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


def _task_s3_prefix_for_version(task_id: str, version: int) -> str:
    return f"tasks/{task_id}/v{version}/"


def _task_archive_key_for_version(task_id: str, version: int) -> str:
    return (
        f"{_task_s3_prefix_for_version(task_id, version)}"
        f"{StorageClient._TASK_ARCHIVE_OBJECT_NAME}"
    )


def _task_overwrite_staging_key(task_id: str) -> str:
    return f"task-upload-staging/{task_id}/{uuid.uuid4().hex}.tar.gz"


def _validate_task_overwrite_staging_key(task_id: str, staging_key: str | None) -> str:
    prefix = f"task-upload-staging/{task_id}/"
    filename = (staging_key or "").removeprefix(prefix)
    token = filename.removesuffix(".tar.gz")
    if (
        not staging_key
        or not staging_key.startswith(prefix)
        or not filename.endswith(".tar.gz")
        or len(token) != 32
        or any(char not in "0123456789abcdef" for char in token)
    ):
        raise HTTPException(
            status_code=400, detail="Invalid task overwrite staging key"
        )
    return staging_key


def _task_overwrite_revision_prefix(
    task_id: str, version: int, staging_key: str
) -> str:
    """Return the immutable source prefix promoted by one overwrite attempt."""
    token = Path(staging_key).name.removesuffix(".tar.gz")
    return f"tasks/{task_id}/v{version}-revisions/{token}/"


async def initialize_task_upload(
    task_name: str,
    *,
    org_id: str | None = None,
    content_hash: str,
    message: str | None = None,
    force_new_version: bool = False,
    overwrite_current_version: bool = False,
) -> TaskUploadInitResponse:
    """Prepare a task upload and return direct-upload details when supported."""
    normalized_name = _normalize_task_name(task_name)

    async with get_session() as session:
        existing_task = await _find_task_by_name(session, normalized_name, org_id)
        target = None
        if existing_task is not None:
            if overwrite_current_version:
                current_id = existing_task.current_version_id
                target = (
                    await session.get(TaskVersionModel, current_id)
                    if current_id
                    else None
                )
                if target is None:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Task {existing_task.id} has no selected version to overwrite",
                    )
            else:
                target = await _latest_version(session, existing_task.id)

        if (
            not force_new_version
            and target is not None
            and target.content_hash
            and target.content_hash == content_hash
        ):
            assert existing_task is not None
            return TaskUploadInitResponse(
                task_id=existing_task.id,
                name=normalized_name,
                s3_key=target.task_s3_key,
                version=target.version,
                version_id=target.id,
                existing_task=True,
                content_unchanged=True,
                content_hash=content_hash,
            )

        if existing_task is not None:
            task_id = existing_task.id
            if overwrite_current_version:
                assert target is not None
                version = target.version
            else:
                version = await _next_version_number(session, task_id)
            existing = True
        else:
            task_id = f"{normalized_name}-{str(uuid.uuid4())[:8]}"
            version = 1
            existing = False

    version_id = f"{task_id}-v{version}"
    s3_key = _task_s3_prefix_for_version(task_id, version)

    storage = get_storage_client()
    staging_key = (
        _task_overwrite_staging_key(task_id)
        if overwrite_current_version and existing
        else None
    )
    upload_key = staging_key or _task_archive_key_for_version(task_id, version)
    try:
        upload_url = await storage.get_presigned_upload_url(
            upload_key,
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
        staging_key=staging_key,
        overwrite_base_content_hash=(
            target.content_hash
            if overwrite_current_version and existing and target is not None
            else None
        ),
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
    overwrite_current_version: bool = False,
    staging_key: str | None = None,
    overwrite_base_content_hash: str | None = None,
) -> UploadResponse:
    """Finalize a direct-to-S3 upload after the client has uploaded bytes.

    When ``register`` is True and the task does not yet exist in the DB, a
    new ``TaskModel`` + v1 ``TaskVersionModel`` pair is created so the task
    becomes visible in the UI without any trials attached. The default
    (``register=False``) preserves the legacy behavior used by the sweep
    path, where the task row is created later by ``create_task``.
    """
    normalized_name = _normalize_task_name(task_name)
    validated_staging_key = (
        _validate_task_overwrite_staging_key(task_id, staging_key)
        if overwrite_current_version
        else None
    )
    s3_key = (
        _task_overwrite_revision_prefix(task_id, version, validated_staging_key)
        if validated_staging_key is not None
        else _task_s3_prefix_for_version(task_id, version)
    )
    archive_key = f"{s3_key}{StorageClient._TASK_ARCHIVE_OBJECT_NAME}"
    upload_key = validated_staging_key or archive_key
    version_id = f"{task_id}-v{version}"
    task_path = f"s3://{s3_key}"

    storage = get_storage_client()
    try:
        archive_exists = await storage.object_exists(upload_key)
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Failed to verify S3 upload: {str(exc)}"
        ) from exc
    async with get_session() as session:
        existing_task = await session.get(
            TaskModel,
            task_id,
            with_for_update=overwrite_current_version,
        )

        if (
            existing_task is not None
            and org_id is not None
            and existing_task.org_id != org_id
        ):
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        if not archive_exists:
            if overwrite_current_version and existing_task is not None:
                completed_version = await session.get(TaskVersionModel, version_id)
                if (
                    existing_task.current_version_id == version_id
                    and completed_version is not None
                    and completed_version.content_hash == content_hash
                ):
                    return UploadResponse(
                        task_id=task_id,
                        name=normalized_name,
                        s3_key=completed_version.task_s3_key or s3_key,
                        version=version,
                        version_id=version_id,
                        existing_task=True,
                        content_hash=content_hash,
                    )
            raise HTTPException(
                status_code=400, detail="Uploaded task archive not found in S3"
            )

        if overwrite_current_version and existing_task is None:
            raise HTTPException(
                status_code=409, detail=f"Task {task_id} has no version to overwrite"
            )

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

            new_version_row = TaskVersionModel(
                id=version_id,
                task_id=task_id,
                version=version,
                task_path=task_path,
                task_s3_key=s3_key,
                content_hash=content_hash,
                message=message,
                created_by_user_id=created_by_user_id,
            )
            session.add(new_version_row)
            await session.flush()

            new_task.current_version_id = version_id

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

        version_row = await session.get(
            TaskVersionModel,
            version_id,
            with_for_update=overwrite_current_version,
        )
        if overwrite_current_version:
            if existing_task.current_version_id != version_id or version_row is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "The selected task version changed during upload; "
                        "start the in-place upload again"
                    ),
                )
            if version_row.content_hash == content_hash:
                try:
                    await storage.delete_prefix(upload_key)
                except Exception:
                    pass
                return UploadResponse(
                    task_id=task_id,
                    name=normalized_name,
                    s3_key=version_row.task_s3_key or s3_key,
                    version=version,
                    version_id=version_id,
                    existing_task=True,
                    content_hash=content_hash,
                )
            if version_row.content_hash != overwrite_base_content_hash:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "The selected task version was overwritten during upload; "
                        "start the in-place upload again"
                    ),
                )
            # Promote into an immutable, attempt-specific prefix. The object is
            # not visible to readers until the version row commits its new
            # ``task_s3_key``, so a failed DB transaction leaves only an
            # unreferenced object rather than split archive/metadata state.
            await storage.copy_object(upload_key, archive_key)
            version_row.task_path = task_path
            version_row.task_s3_key = s3_key
            version_row.content_hash = content_hash
            if message is not None:
                version_row.message = message
            version_row.expanded_at = None
            version_row.expanded_manifest_key = None
            version_row.pre_trial = None
            version_row.pre_trial_status = None
            version_row.pre_trial_error = None
            version_row.pre_trial_started_at = None
            version_row.pre_trial_finished_at = None

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

        await session.commit()

    if overwrite_current_version:
        # Both are derived/unreferenced after the DB switch. Readers verify an
        # expanded manifest's source archive before selecting it, so cleanup
        # failures cannot make stale files authoritative.
        for prefix in (f"tasks/{task_id}/v{version}-files/", upload_key):
            try:
                await storage.delete_prefix(prefix)
            except Exception:
                pass

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

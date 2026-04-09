from __future__ import annotations

import hashlib
import shutil
import tarfile
import tempfile
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import settings
from oddish.db import TaskModel, TaskVersionModel, get_session
from oddish.db.storage import StorageClient, extract_task_tarfile, get_storage_client
from oddish.schemas import TaskUploadInitResponse, UploadResponse
from oddish.task_timeouts import (
    TaskTimeoutValidationError,
    validate_task_timeout_config,
)


def _compute_file_hash(path: Path) -> str:
    """Return the SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


async def _write_upload_to_file(
    file: UploadFile, destination: Path, *, max_bytes: int
) -> int:
    total = 0
    chunk_size = 1024 * 1024
    with destination.open("wb") as handle:
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes and total > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"Task upload too large. Max size is {settings.max_task_upload_mb}MB."
                    ),
                )
            handle.write(chunk)
    return total


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
) -> TaskUploadInitResponse:
    """Prepare a task upload and return direct-upload details when supported."""
    normalized_name = _normalize_task_name(task_name)

    async with get_session() as session:
        existing_task = await _find_task_by_name(session, normalized_name, org_id)
        latest = (
            await _latest_version(session, existing_task.id) if existing_task is not None else None
        )

        if (
            latest is not None
            and latest.content_hash
            and latest.content_hash == content_hash
        ):
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
) -> UploadResponse:
    """Finalize a direct-to-S3 upload after the client has uploaded bytes."""
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
        if existing_task is None:
            return UploadResponse(
                task_id=task_id,
                name=normalized_name,
                s3_key=s3_key,
                version=version,
                version_id=version_id,
                content_hash=content_hash,
            )

        if org_id is not None and existing_task.org_id != org_id:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        version_row = await session.get(TaskVersionModel, version_id)
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

        existing_task.task_path = task_path
        existing_task.task_s3_key = s3_key
        existing_task.current_version_id = version_id
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


async def _handle_existing_task_upload(
    existing_task: TaskModel,
    *,
    task_name: str,
    tarball_path: Path,
    content_hash: str,
    message: str | None,
    created_by_user_id: str | None,
) -> UploadResponse:
    """Handle an upload for a task name that already exists.

    Compares *content_hash* with the latest version's hash.  If identical the
    current version is returned without creating a new one.  Otherwise a new
    version row + storage artefact is created.
    """
    task_id = existing_task.id

    async with get_session() as session:
        latest = await _latest_version(session, task_id)

        # Content unchanged -- reuse existing version
        if (
            latest is not None
            and latest.content_hash
            and latest.content_hash == content_hash
        ):
            return UploadResponse(
                task_id=task_id,
                name=task_name,
                s3_key=latest.task_s3_key,
                version=latest.version,
                version_id=latest.id,
                existing_task=True,
                content_unchanged=True,
                content_hash=content_hash,
            )

        # Content changed -- create new version
        version = await _next_version_number(session, task_id)
        version_id = f"{task_id}-v{version}"
        storage = get_storage_client()
        try:
            s3_key = await storage.upload_task_archive_versioned(
                task_id, version, tarball_path
            )
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Failed to upload to S3: {str(e)}"
            )
        task_path = f"s3://{s3_key}"

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

        # Refresh the task within this session to update mutable fields
        task = await session.get(TaskModel, task_id)
        if task is not None:
            task.task_path = task_path
            task.task_s3_key = s3_key
            task.current_version_id = version_id

        await session.commit()

    return UploadResponse(
        task_id=task_id,
        name=task_name,
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
    """
    storage = get_storage_client()

    # Try versioned prefix first
    if version is not None:
        versioned_key = f"tasks/{task_id}/v{version}/"
        try:
            if await storage.prefix_exists(versioned_key):
                return f"s3://{versioned_key}", versioned_key
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Failed to check S3: {str(e)}"
            )

    # Fall back to legacy un-versioned prefix
    task_s3_key = f"tasks/{task_id}/"
    try:
        exists = await storage.prefix_exists(task_s3_key)
        if not exists:
            raise HTTPException(
                status_code=404,
                detail=s3_missing_detail or local_missing_detail or f"Task {task_id} not found in S3",
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to check S3: {str(e)}")

    return f"s3://{task_s3_key}", task_s3_key

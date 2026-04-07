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
from oddish.db.storage import extract_task_tarfile, get_storage_client
from oddish.schemas import UploadResponse
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


async def handle_task_upload(
    file: UploadFile,
    *,
    org_id: str | None = None,
    content_hash: str | None = None,
    message: str | None = None,
    created_by_user_id: str | None = None,
) -> UploadResponse:
    """Upload a task tarball to S3 or local storage.

    The handler automatically resolves whether a task with the same name (scoped
    to *org_id*) already exists:

    * **Existing task, same content** -- returns the current version without
      creating a new one (``content_unchanged=True``).
    * **Existing task, different content** -- creates a new version.
    * **New task** -- stores v1 for later ``create_task`` in the sweep endpoint.
    """
    original_filename = file.filename or "task.tar.gz"
    name_stem = Path(original_filename).stem
    if name_stem.endswith(".tar"):
        name_stem = Path(name_stem).stem

    task_name = name_stem

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        task_dir = tmpdir_path / "task"
        task_dir.mkdir()

        tarball_path = tmpdir_path / "task.tar.gz"
        max_bytes = max(settings.max_task_upload_mb, 0) * 1024 * 1024
        await _write_upload_to_file(file, tarball_path, max_bytes=max_bytes)

        try:
            with tarfile.open(tarball_path, "r:gz") as tar:
                extract_task_tarfile(tar, task_dir)
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid tarball: {str(e)}")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid tarball: {str(e)}")

        try:
            validate_task_timeout_config(task_dir)
        except TaskTimeoutValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Prefer the deterministic hash sent by the CLI; fall back to hashing
        # the raw tarball for backward-compat with older clients / direct API.
        if not content_hash:
            content_hash = _compute_file_hash(tarball_path)

        # ----- Check if a task with this name already exists -----
        async with get_session() as session:
            existing_task = await _find_task_by_name(session, task_name, org_id)

        if existing_task is not None:
            return await _handle_existing_task_upload(
                existing_task,
                task_name=task_name,
                tarball_path=tarball_path,
                task_dir=task_dir,
                content_hash=content_hash,
                message=message,
                created_by_user_id=created_by_user_id,
            )

        # ----- Brand-new task (first version created later in create_task) -----
        task_id = f"{name_stem}-{str(uuid.uuid4())[:8]}"

        if settings.s3_enabled:
            storage = get_storage_client()
            try:
                s3_key = await storage.upload_task_archive_versioned(
                    task_id, 1, tarball_path
                )
                return UploadResponse(
                    task_id=task_id,
                    name=task_name,
                    s3_key=s3_key,
                    version=1,
                    content_hash=content_hash,
                )
            except Exception as e:
                raise HTTPException(
                    status_code=500, detail=f"Failed to upload to S3: {str(e)}"
                )

        local_storage = Path(settings.local_storage_dir)
        local_storage.mkdir(parents=True, exist_ok=True)
        task_storage_path = local_storage / task_id / "v1"
        shutil.copytree(task_dir, task_storage_path)
        return UploadResponse(
            task_id=task_id,
            name=task_name,
            task_path=str(task_storage_path),
            version=1,
            content_hash=content_hash,
        )


async def _handle_existing_task_upload(
    existing_task: TaskModel,
    *,
    task_name: str,
    tarball_path: Path,
    task_dir: Path,
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
                task_path=latest.task_path if not settings.s3_enabled else None,
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

        if settings.s3_enabled:
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
        else:
            local_storage = Path(settings.local_storage_dir)
            local_storage.mkdir(parents=True, exist_ok=True)
            task_storage_path = local_storage / task_id / f"v{version}"
            shutil.copytree(task_dir, task_storage_path)
            task_path = str(task_storage_path)
            s3_key = None

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
        task_path=task_path if not settings.s3_enabled else None,
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
    """Resolve task path based on storage mode, verifying existence.

    When *version* is given the versioned prefix ``tasks/{task_id}/v{version}/``
    is checked first.  Falls back to the legacy un-versioned prefix for
    backwards compatibility with tasks uploaded before versioning.
    """
    if settings.s3_enabled:
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
                    detail=s3_missing_detail or f"Task {task_id} not found in S3",
                )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to check S3: {str(e)}")

        return f"s3://{task_s3_key}", task_s3_key

    # Local storage — check versioned path first
    if version is not None:
        versioned_path = Path(settings.local_storage_dir) / task_id / f"v{version}"
        if versioned_path.exists():
            return str(versioned_path), None

    local_storage = Path(settings.local_storage_dir) / task_id
    if not local_storage.exists():
        raise HTTPException(
            status_code=404,
            detail=local_missing_detail or f"Task {task_id} not found",
        )

    return str(local_storage), None

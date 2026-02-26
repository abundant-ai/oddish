from __future__ import annotations

import secrets

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from oddish.api.helpers import (
    build_task_status_responses_from_counts,
    build_trial_response,
)
from oddish.config import settings
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TrialModel,
    get_storage_client,
)
from oddish.schemas import TaskStatusResponse, TrialResponse


def generate_public_token() -> str:
    """Generate a URL-safe token for public sharing."""
    return secrets.token_urlsafe(32)


async def ensure_experiment_public(
    session: AsyncSession, experiment: ExperimentModel
) -> None:
    """Ensure an experiment is published with a unique public token."""
    if experiment.is_public:
        return
    if not experiment.public_token:
        for _ in range(5):
            candidate = generate_public_token()
            exists = await session.execute(
                select(ExperimentModel.id).where(
                    ExperimentModel.public_token == candidate
                )
            )
            if exists.scalar_one_or_none() is None:
                experiment.public_token = candidate
                break
        if not experiment.public_token:
            raise HTTPException(
                status_code=500, detail="Failed to generate unique share token"
            )
    experiment.is_public = True


# =============================================================================
# Database Access Helpers
# =============================================================================


async def get_public_experiment(
    session: AsyncSession, public_token: str
) -> ExperimentModel | None:
    """Get a public experiment by its share token."""
    result = await session.execute(
        select(ExperimentModel)
        .where(ExperimentModel.public_token == public_token)
        .where(ExperimentModel.is_public == True)  # noqa: E712
    )
    return result.scalar_one_or_none()


async def get_public_task(session: AsyncSession, task_id: str) -> TaskModel | None:
    """Get a task that belongs to a public experiment."""
    result = await session.execute(
        select(TaskModel)
        .options(selectinload(TaskModel.trials), selectinload(TaskModel.experiment))
        .join(ExperimentModel)
        .where(TaskModel.id == task_id)
        .where(ExperimentModel.is_public == True)  # noqa: E712
    )
    return result.scalar_one_or_none()


async def get_public_trial(session: AsyncSession, trial_id: str) -> TrialModel | None:
    """Get a trial that belongs to a public experiment."""
    result = await session.execute(
        select(TrialModel)
        .join(TaskModel, TaskModel.id == TrialModel.task_id)
        .join(ExperimentModel, ExperimentModel.id == TaskModel.experiment_id)
        .where(TrialModel.id == trial_id)
        .where(ExperimentModel.is_public == True)  # noqa: E712
    )
    return result.scalar_one_or_none()


async def get_task_status_counts(
    session: AsyncSession,
    task_id: str,
    filters: list,
    *,
    join_experiment: bool = False,
) -> TaskStatusResponse:
    """Get task status with aggregated trial counts."""
    query = select(TaskModel).where(TaskModel.id == task_id)
    if join_experiment:
        query = query.join(
            ExperimentModel, ExperimentModel.id == TaskModel.experiment_id
        )
    for clause in filters:
        query = query.where(clause)

    result = await session.execute(query)
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    return (await build_task_status_responses_from_counts(session, tasks=[task]))[0]


async def list_task_trials_for_task(
    session: AsyncSession, task_id: str
) -> list[TrialResponse]:
    """List all trials for a task with their responses."""
    result = await session.execute(
        select(TrialModel, TaskModel.task_path)
        .join(TaskModel, TaskModel.id == TrialModel.task_id)
        .where(TrialModel.task_id == task_id)
        .order_by(TrialModel.created_at.asc())
    )
    rows = result.all()
    return [build_trial_response(trial, task_path) for trial, task_path in rows]


# =============================================================================
# S3 File Operations
# =============================================================================


async def list_task_files_s3(
    task_id: str,
    prefix: str | None,
    recursive: bool,
    limit: int,
    cursor: str | None,
    presign: bool,
) -> dict:
    """List files in a task's S3 directory."""
    if not settings.s3_enabled:
        raise HTTPException(status_code=501, detail="S3 storage not enabled")

    storage = get_storage_client()

    try:
        return await storage.list_task_files(
            task_id=task_id,
            prefix=prefix,
            recursive=recursive,
            limit=limit,
            cursor=cursor,
            presign=presign,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list files: {str(e)}")


async def get_task_file_content_s3(
    task_id: str,
    file_path: str,
    presign: bool,
) -> dict:
    """Get content of a specific task file from S3."""
    if not settings.s3_enabled:
        raise HTTPException(status_code=501, detail="S3 storage not enabled")

    storage = get_storage_client()

    try:
        return await storage.get_task_file_content(
            task_id=task_id,
            file_path=file_path,
            presign=presign,
        )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=404, detail="File not found")


def _get_trial_s3_prefix(trial: TrialModel) -> str:
    from oddish.db.storage import StorageClient

    return trial.trial_s3_key or StorageClient._trial_prefix(trial.id)


async def list_trial_files_s3(
    trial: TrialModel,
    presign: bool = True,
    presign_expiration: int = 900,
) -> dict:
    """List all files in a trial's S3 directory with presigned URLs."""
    if not settings.s3_enabled:
        raise HTTPException(status_code=501, detail="S3 storage not enabled")

    storage = get_storage_client()
    s3_prefix = _get_trial_s3_prefix(trial)

    try:
        objects = await storage.list_objects_all(s3_prefix)
        files = []
        for obj in objects:
            key = obj.get("key")
            if not key:
                continue
            relative_path = key[len(s3_prefix) :]
            if relative_path:
                files.append(
                    {
                        "path": relative_path,
                        "key": key,
                        "size": obj.get("size"),
                        "last_modified": obj.get("last_modified"),
                    }
                )

        if presign and files:
            s3_keys = [f["key"] for f in files]
            urls = await storage.get_presigned_urls_batch(s3_keys, presign_expiration)
            for f in files:
                f["url"] = urls.get(f["key"])

        return {
            "trial_id": trial.id,
            "files": files,
            "prefix": s3_prefix,
            "presigned": presign,
            "presign_expires_in": presign_expiration if presign else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to list trial files: {str(e)}"
        )


async def get_trial_file_content_s3(
    trial: TrialModel,
    file_path: str,
) -> tuple[bytes, str]:
    """Download a file from a trial's S3 directory by relative path."""
    import mimetypes
    from pathlib import PurePosixPath

    if not settings.s3_enabled:
        raise HTTPException(status_code=501, detail="S3 storage not enabled")

    raw = file_path.replace("\\", "/").strip()
    if not raw or raw.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid file path")
    parts = PurePosixPath(raw).parts
    if ".." in parts:
        raise HTTPException(status_code=400, detail="Invalid file path")
    normalized = str(PurePosixPath(*parts))

    media_type, _ = mimetypes.guess_type(normalized)
    if media_type is None:
        media_type = "application/octet-stream"

    storage = get_storage_client()
    s3_prefix = _get_trial_s3_prefix(trial)
    s3_key = f"{s3_prefix}{normalized}"

    try:
        content = await storage.download_bytes(s3_key)
        return content, media_type
    except Exception:
        raise HTTPException(status_code=404, detail="File not found")

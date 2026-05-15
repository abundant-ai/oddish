from __future__ import annotations

import secrets

from fastapi import HTTPException
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from oddish.core.helpers import (
    build_task_status_responses_from_counts,
    build_trial_response,
    fetch_trial_queue_info,
)
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TrialModel,
    get_storage_client,
    task_experiments,
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


async def get_public_task(
    session: AsyncSession,
    task_id: str,
    *,
    load_current_version: bool = False,
) -> TaskModel | None:
    """Get a task that belongs to at least one public experiment.

    Pass ``load_current_version=True`` from callers that read
    ``task.current_version`` (the public file-serving routes).
    Default is False so the public task-status path doesn't pay for
    an extra ``task_versions`` round trip it never reads.
    """
    public_link_exists = exists(
        select(1)
        .select_from(
            task_experiments.join(
                ExperimentModel,  # type: ignore[arg-type]
                ExperimentModel.id == task_experiments.c.experiment_id,
            )
        )
        .where(
            task_experiments.c.task_id == TaskModel.id,
            ExperimentModel.is_public == True,  # noqa: E712
        )
    )
    options = [selectinload(TaskModel.trials), selectinload(TaskModel.experiments)]
    if load_current_version:
        options.append(selectinload(TaskModel.current_version))
    result = await session.execute(
        select(TaskModel)
        .options(*options)
        .where(TaskModel.id == task_id)
        .where(public_link_exists)
    )
    return result.scalar_one_or_none()


async def get_public_trial(session: AsyncSession, trial_id: str) -> TrialModel | None:
    """Get a trial whose experiment is public."""
    result = await session.execute(
        select(TrialModel)
        .join(ExperimentModel, ExperimentModel.id == TrialModel.experiment_id)
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
            task_experiments, task_experiments.c.task_id == TaskModel.id
        ).join(
            ExperimentModel,
            ExperimentModel.id == task_experiments.c.experiment_id,
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
    """List all trials for a task with their responses.

    Superseded trials (rows replaced by a user-driven retry) are
    hidden by default so the public trial list collapses the rerun
    chain down to the live attempt -- matching what
    ``get_task_status_trials`` returns for the dashboard.
    """
    result = await session.execute(
        select(TrialModel, TaskModel.task_path)
        .join(TaskModel, TaskModel.id == TrialModel.task_id)
        .where(
            TrialModel.task_id == task_id,
            TrialModel.superseded_by_trial_id.is_(None),
        )
        .order_by(TrialModel.created_at.asc())
    )
    rows = result.all()
    trials = [trial for trial, _ in rows]
    queue_info_by_trial_id = await fetch_trial_queue_info(session, trials=trials)
    return [
        build_trial_response(
            trial,
            task_path,
            queue_info=queue_info_by_trial_id.get(trial.id),
        )
        for trial, task_path in rows
    ]


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
    version: int | None = None,
) -> dict:
    """List files in a task's S3 directory."""
    storage = get_storage_client()

    try:
        return await storage.list_task_files(
            task_id=task_id,
            prefix=prefix,
            recursive=recursive,
            limit=limit,
            cursor=cursor,
            presign=presign,
            version=version,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list files: {str(e)}")


async def get_task_file_content_s3(
    task_id: str,
    file_path: str,
    presign: bool,
    version: int | None = None,
) -> dict:
    """Get content of a specific task file from S3."""
    storage = get_storage_client()

    try:
        return await storage.get_task_file_content(
            task_id=task_id,
            file_path=file_path,
            presign=presign,
            version=version,
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
    prefix: str | None = None,
    recursive: bool = True,
    limit: int = 1000,
    cursor: str | None = None,
    presign: bool = True,
    presign_expiration: int = 900,
) -> dict:
    """List files in a trial's S3 directory with optional presigned URLs."""
    storage = get_storage_client()

    try:
        return await storage.list_trial_files(
            trial_id=trial.id,
            prefix=prefix,
            recursive=recursive,
            limit=limit,
            cursor=cursor,
            presign=presign,
            presign_expiration=presign_expiration,
        )
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

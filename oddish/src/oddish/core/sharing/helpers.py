from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value

from oddish.core.experiment_membership import trial_in_experiment
from oddish.core.helpers import (
    build_task_status_responses_from_counts,
    build_trial_response,
    fetch_trial_queue_info,
)
from oddish.core.model_display_names import (
    apply_model_display_names,
    load_model_display_names,
)
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    experiment_trials,
    get_session,
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


async def get_public_task_for_experiment(
    session: AsyncSession,
    public_token: str,
    task_id: str,
    *,
    load_current_version: bool = False,
) -> tuple[ExperimentModel, TaskModel, set[str]] | None:
    """Get a public task only through the share token that exposes it."""
    experiment = await get_public_experiment(session, public_token)
    if not experiment:
        return None

    membership_exists = exists(
        select(1)
        .select_from(task_experiments)
        .where(
            task_experiments.c.task_id == TaskModel.id,
            task_experiments.c.experiment_id == experiment.id,
            task_experiments.c.deleted_at.is_(None),
        )
    )
    options = [selectinload(TaskModel.trials), selectinload(TaskModel.experiments)]
    if load_current_version:
        options.append(selectinload(TaskModel.current_version))
    result = await session.execute(
        select(TaskModel)
        .options(*options)
        .where(TaskModel.id == task_id)
        .where(membership_exists)
    )
    task = result.scalar_one_or_none()
    if task is None:
        return None

    gathered_ids = set(
        (
            await session.execute(
                select(experiment_trials.c.trial_id).where(
                    experiment_trials.c.experiment_id == experiment.id,
                    experiment_trials.c.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    set_committed_value(
        task,
        "trials",
        [
            t
            for t in task.trials
            if not t.is_probe
            and (t.experiment_id == experiment.id or t.id in gathered_ids)
        ],
    )
    return experiment, task, gathered_ids


async def get_public_trial_for_experiment(
    session: AsyncSession, public_token: str, trial_id: str
) -> TrialModel | None:
    """Get a public trial only through the share token that exposes it."""
    experiment = await get_public_experiment(session, public_token)
    if not experiment:
        return None
    result = await session.execute(
        select(TrialModel)
        .where(TrialModel.id == trial_id)
        .where(TrialModel.is_probe.is_(False))
        .where(trial_in_experiment(experiment.id))
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
    # ``build_task_status_responses_from_counts`` aggregates trials in SQL
    # but its response builder still reads ``task.experiments``.
    query = (
        select(TaskModel)
        .options(selectinload(TaskModel.experiments))
        .where(TaskModel.id == task_id)
    )
    if join_experiment:
        query = query.join(
            task_experiments, task_experiments.c.task_id == TaskModel.id
        ).join(
            ExperimentModel,
            ExperimentModel.id == task_experiments.c.experiment_id,
        )
        query = query.where(task_experiments.c.deleted_at.is_(None))
        # A task can belong to several matching experiments; without
        # DISTINCT the join yields one row per membership and
        # scalar_one_or_none() raises MultipleResultsFound.
        query = query.distinct()
    for clause in filters:
        query = query.where(clause)

    result = await session.execute(query)
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    return (await build_task_status_responses_from_counts(session, tasks=[task]))[0]


async def list_experiment_trials_for_org(
    session: AsyncSession, experiment_id: str, org_id: str | None
) -> list[TrialResponse]:
    """List non-superseded trials for an experiment (org-scoped)."""
    conditions = [
        trial_in_experiment(experiment_id),
        TrialModel.superseded_by_trial_id.is_(None),
    ]
    if org_id is not None:
        conditions.append(TrialModel.org_id == org_id)
    result = await session.execute(
        select(TrialModel, TaskModel.task_path)
        .join(TaskModel, TaskModel.id == TrialModel.task_id)
        .where(*conditions)
        .order_by(TrialModel.created_at.asc())
    )
    rows = result.all()
    trials = [trial for trial, _ in rows]
    queue_info_by_trial_id = await fetch_trial_queue_info(session, trials=trials)
    return [
        build_trial_response(
            trial, task_path, queue_info=queue_info_by_trial_id.get(trial.id)
        )
        for trial, task_path in rows
    ]


async def list_task_trials_for_task(
    session: AsyncSession,
    task_id: str,
    *,
    probe: bool | None = None,
    version: int | None = None,
) -> list[TrialResponse]:
    """List all trials for a task with their responses.

    Superseded trials (rows replaced by a user-driven retry) are
    hidden by default so the public trial list collapses the rerun
    chain down to the live attempt -- matching what
    ``get_task_status_trials`` returns for the dashboard.

    ``probe`` filters by trial kind: True -> only probe trials, False ->
    only real attempts, None -> all.

    ``version`` scopes to trials of one task version. A task can carry
    trials across many versions and experiments, each row with its full
    analysis payload, so version-scoped callers (the task overview) must
    filter here rather than shipping everything to the client. The inner
    join deliberately drops unversioned trials — they belong to no
    version, so no version-scoped view should include them.
    """
    conditions = [
        TrialModel.task_id == task_id,
        TrialModel.superseded_by_trial_id.is_(None),
    ]
    if probe is not None:
        conditions.append(TrialModel.is_probe == probe)
    query = select(TrialModel, TaskModel.task_path).join(
        TaskModel, TaskModel.id == TrialModel.task_id
    )
    if version is not None:
        query = query.join(
            TaskVersionModel, TaskVersionModel.id == TrialModel.task_version_id
        )
        conditions.append(TaskVersionModel.version == version)
    result = await session.execute(
        query.where(*conditions).order_by(TrialModel.created_at.asc())
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


async def list_task_trials_for_public_experiment(
    session: AsyncSession, public_token: str, task_id: str
) -> list[TrialResponse] | None:
    """List real-attempt task trials visible through one public share token."""
    resolved = await get_public_task_for_experiment(session, public_token, task_id)
    if resolved is None:
        return None
    experiment, _, _ = resolved
    result = await session.execute(
        select(TrialModel, TaskModel.task_path)
        .join(TaskModel, TaskModel.id == TrialModel.task_id)
        .where(
            TrialModel.task_id == task_id,
            TrialModel.superseded_by_trial_id.is_(None),
            TrialModel.is_probe.is_(False),
            trial_in_experiment(experiment.id),
        )
        .order_by(TrialModel.created_at.asc())
    )
    rows = result.all()
    trials = [trial for trial, _ in rows]
    queue_info_by_trial_id = await fetch_trial_queue_info(session, trials=trials)
    responses = [
        build_trial_response(
            trial,
            task_path,
            queue_info=queue_info_by_trial_id.get(trial.id),
        )
        for trial, task_path in rows
    ]
    apply_model_display_names(responses, await load_model_display_names(session))
    return responses


# =============================================================================
# S3 File Operations
# =============================================================================


async def _task_version_s3_prefix(task_id: str, version: int | None) -> str | None:
    """Resolve the DB-selected source prefix for a task version."""
    if version is None:
        return None
    async with get_session() as session:
        row = await session.scalar(
            select(TaskVersionModel.task_s3_key).where(
                TaskVersionModel.task_id == task_id,
                TaskVersionModel.version == version,
            )
        )
    return str(row) if row else None


async def list_task_files_s3(
    task_id: str,
    prefix: str | None,
    recursive: bool,
    limit: int,
    cursor: str | None,
    presign: bool,
    version: int | None = None,
    inline: bool = True,
) -> dict:
    """List files in a task's S3 directory."""
    storage = get_storage_client()

    try:
        task_s3_prefix = await _task_version_s3_prefix(task_id, version)
        return await storage.list_task_files(
            task_id=task_id,
            prefix=prefix,
            recursive=recursive,
            limit=limit,
            cursor=cursor,
            presign=presign,
            version=version,
            task_s3_prefix=task_s3_prefix,
            inline=inline,
        )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to list files")


async def stream_task_files_s3(
    task_id: str,
    prefix: str | None,
    recursive: bool,
    limit: int,
    cursor: str | None,
    presign: bool,
    version: int | None = None,
):
    """Stream a task file listing chunk-by-chunk (tree first, then contents).

    Errors before the first chunk surface as HTTP errors; a failure
    mid-stream just ends the stream — the client already has the tree and
    falls back to per-file fetches for missing bodies.
    """
    storage = get_storage_client()
    task_s3_prefix = await _task_version_s3_prefix(task_id, version)

    stream = storage.stream_task_files(
        task_id=task_id,
        prefix=prefix,
        recursive=recursive,
        limit=limit,
        cursor=cursor,
        presign=presign,
        version=version,
        task_s3_prefix=task_s3_prefix,
    )
    started = False
    try:
        async for chunk in stream:
            started = True
            yield chunk
    except HTTPException:
        if not started:
            raise
    except Exception:
        if not started:
            raise HTTPException(status_code=500, detail="Failed to list files")


def _ndjson_line(chunk: dict) -> str:
    return json.dumps(jsonable_encoder(chunk), separators=(",", ":")) + "\n"


async def make_task_files_ndjson_response(
    stream: AsyncIterator[dict],
) -> StreamingResponse:
    """Wrap a task-files chunk stream as an NDJSON streaming response.

    The first chunk (the listing) is awaited eagerly, before the response
    starts, so failures during listing — task not found, storage errors —
    propagate as real HTTP error responses. Once the body iterator is
    running Starlette has already sent a 200, so only mid-stream failures
    end up truncating the stream (the client keeps the tree and falls back
    to per-file fetches for missing bodies).
    """
    try:
        first = await anext(stream)
    except StopAsyncIteration:
        first = None

    async def ndjson() -> AsyncIterator[str]:
        if first is None:
            return
        yield _ndjson_line(first)
        async for chunk in stream:
            yield _ndjson_line(chunk)

    return StreamingResponse(ndjson(), media_type="application/x-ndjson")


async def get_task_file_content_s3(
    task_id: str,
    file_path: str,
    presign: bool,
    version: int | None = None,
    max_bytes: int | None = None,
) -> dict:
    """Get content of a specific task file from S3."""
    storage = get_storage_client()

    try:
        task_s3_prefix = await _task_version_s3_prefix(task_id, version)
        return await storage.get_task_file_content(
            task_id=task_id,
            file_path=file_path,
            presign=presign,
            version=version,
            task_s3_prefix=task_s3_prefix,
            max_bytes=max_bytes,
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
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to list trial files")


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

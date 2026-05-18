from __future__ import annotations

import asyncio
from collections import Counter
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_policy import (
    ALLOWED_CLOUD_ENVIRONMENTS,
    get_default_cloud_environment,
)
from oddish.core.endpoints import (
    browse_tasks_core,
    create_task_sweep_core,
    delete_experiment_core,
    get_task_detail_core,
    get_task_for_org_core,
    get_task_status_core,
    get_task_version_core,
    list_tasks_core,
    list_task_versions_core,
    rerun_task_analysis_core,
    rerun_task_verdict_core,
)
from oddish.core.dashboard import invalidate_dashboard_cache
from oddish.core.public_helpers import (
    ensure_experiment_public,
    get_task_file_content_s3,
    list_task_files_s3,
)
from api.schemas import (
    ExperimentShareResponse,
    ExperimentUpdateRequest,
    ExperimentUpdateResponse,
)
from auth import APIKeyScope, AuthContext, require_admin, require_auth
from models import APIKeyModel, UserModel
from oddish.core.tasks import (
    complete_task_upload,
    initialize_task_upload,
)
from oddish.db import (
    ExperimentModel,
    TaskModel,
    get_session,
)
from oddish.timing import TimingRecorder, add_server_timing_metric, elapsed_ms, now
from oddish.queue import (
    cancel_tasks_runs,
)
from oddish.schemas import (
    TaskBrowseResponse,
    TaskBatchCancelRequest,
    TaskDetailResponse,
    TaskUploadCompleteRequest,
    TaskUploadInitRequest,
    TaskUploadInitResponse,
    TaskResponse,
    TaskStatusResponse,
    TaskSweepSubmission,
    TaskVersionResponse,
    UploadResponse,
)

router = APIRouter(tags=["Tasks"])
logger = logging.getLogger(__name__)


def _make_timing_recorder(request: Request) -> TimingRecorder:
    def _record(name: str, duration_ms: float, description: str | None = None) -> None:
        add_server_timing_metric(request, name, duration_ms, description)

    return _record


MODAL_CANCEL_BATCH_SIZE = 32


async def _cancel_modal_function_calls(modal_fc_ids: list[str]) -> int:
    if not modal_fc_ids:
        return 0

    try:
        import modal
    except ImportError:
        return 0

    unique_fc_ids = list(dict.fromkeys(modal_fc_ids))
    cancelled = 0

    async def cancel_one(fc_id: str) -> bool:
        try:
            fc = modal.FunctionCall.from_id(fc_id)
            await fc.cancel.aio(terminate_containers=True)
            return True
        except Exception:
            return False

    for start in range(0, len(unique_fc_ids), MODAL_CANCEL_BATCH_SIZE):
        batch = unique_fc_ids[start : start + MODAL_CANCEL_BATCH_SIZE]
        results = await asyncio.gather(*(cancel_one(fc_id) for fc_id in batch))
        cancelled += sum(1 for result in results if result)

    return cancelled


def _apply_github_attribution(submission: TaskSweepSubmission) -> None:
    if submission.github_username:
        submission.tags = submission.tags or {}
        submission.tags.setdefault("github_username", submission.github_username)


async def _resolve_actor_user(
    session: AsyncSession,
    auth: AuthContext,
) -> UserModel | None:
    """Return the UserModel of the authenticating principal, or None.

    The auth dependency caches lightweight identity tuples — on cache hits
    the ORM ``user`` / ``api_key`` objects are stripped and only the IDs are
    available, so we lazy-load via ``session.get`` when needed.
    """
    if auth.user is not None:
        return auth.user
    if auth.user_id:
        user = await session.get(UserModel, auth.user_id)
        if user is not None:
            return user
    if auth.api_key_id:
        api_key = auth.api_key or await session.get(APIKeyModel, auth.api_key_id)
        if api_key and api_key.created_by_user_id:
            return await session.get(UserModel, api_key.created_by_user_id)
    return None


async def _resolve_actor_user_string(
    session: AsyncSession,
    auth: AuthContext,
    explicit_user: str | None,
    explicit_github_username: str | None,
) -> str:
    """Resolve a non-empty author string from the authenticated actor.

    Precedence:
      1. explicit_user (e.g. --user)
      2. explicit_github_username (e.g. --github-user)
      3. actor's UserModel.email (the stable Clerk-backed identity)
      4. api_key.name (service-account API keys with no linked user)
      5. "unknown" (so tasks.user is never empty)
    """
    if explicit_user:
        return explicit_user
    if explicit_github_username:
        return explicit_github_username

    actor = await _resolve_actor_user(session, auth)
    if actor and actor.email:
        return actor.email

    if auth.api_key_id:
        api_key = auth.api_key or await session.get(APIKeyModel, auth.api_key_id)
        if api_key and api_key.name:
            return api_key.name

    return "unknown"


async def _resolve_submission_identity(
    session: AsyncSession,
    submission: TaskSweepSubmission,
    auth: AuthContext,
) -> None:
    """Fill submission.user and submission.github_username from the authenticated
    actor when missing. Mutates submission in place.

    `github_username` is only auto-filled from UserModel.github_username so the
    dashboard's `source: "github"` attribution stays meaningful.
    """
    if not submission.github_username:
        actor = await _resolve_actor_user(session, auth)
        if actor and actor.github_username:
            submission.github_username = actor.github_username

    submission.user = await _resolve_actor_user_string(
        session,
        auth,
        explicit_user=submission.user,
        explicit_github_username=submission.github_username,
    )


async def _resolve_created_by_user_id(
    session: AsyncSession,
    submission: TaskSweepSubmission,
    auth: AuthContext,
) -> str | None:
    if auth.api_key_id:
        api_key = auth.api_key
        if api_key is None:
            api_key = await session.get(APIKeyModel, auth.api_key_id)
        if api_key and api_key.created_by_user_id:
            return api_key.created_by_user_id

    if submission.github_username:
        user_result = await session.execute(
            select(UserModel).where(
                UserModel.github_username == submission.github_username,
                UserModel.org_id == auth.org_id,
                UserModel.is_active == True,  # noqa: E712
            )
        )
        user = user_result.scalar_one_or_none()
        if user:
            return user.id

    return None


async def _maybe_publish_experiment(
    session: AsyncSession,
    task: TaskModel,
    submission: TaskSweepSubmission,
    auth: AuthContext,
) -> None:
    should_publish = submission.publish_experiment
    if should_publish is None:
        should_publish = bool(submission.github_username and auth.api_key_id)
    if not should_publish:
        return

    experiments = list(task.experiments or [])
    for experiment in experiments:
        await ensure_experiment_public(session, experiment)


# =============================================================================
# Task Upload and Creation
# =============================================================================


@router.post("/tasks/upload/init", response_model=TaskUploadInitResponse)
async def init_task_upload(
    payload: TaskUploadInitRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> TaskUploadInitResponse:
    """Prepare a task upload and return a presigned PUT URL when S3 is enabled."""
    auth.require_scope(APIKeyScope.TASKS)
    return await initialize_task_upload(
        payload.name,
        org_id=auth.org_id,
        content_hash=payload.content_hash,
        message=payload.message,
        force_new_version=payload.force_new_version,
    )


@router.post("/tasks/upload/complete", response_model=UploadResponse)
async def finalize_task_upload(
    payload: TaskUploadCompleteRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> UploadResponse:
    """Finalize a direct task upload after the client PUTs the archive to S3."""
    auth.require_scope(APIKeyScope.TASKS)

    resolved_user = payload.user
    if payload.register_task and not resolved_user:
        async with get_session() as session:
            resolved_user = await _resolve_actor_user_string(
                session,
                auth,
                explicit_user=payload.user,
                explicit_github_username=None,
            )

    return await complete_task_upload(
        task_id=payload.task_id,
        task_name=payload.name,
        version=payload.version,
        content_hash=payload.content_hash,
        message=payload.message,
        org_id=auth.org_id,
        created_by_user_id=auth.user_id,
        register=payload.register_task,
        user=resolved_user,
        priority=payload.priority,
    )


@router.post("/tasks/sweep", response_model=TaskResponse)
async def create_task_sweep(
    submission: TaskSweepSubmission,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> TaskResponse:
    """Submit a task sweep - expands a task_id into many trials."""
    auth.require_scope(APIKeyScope.TASKS)

    from oddish.core.sweeps import validate_sweep_submission

    validate_sweep_submission(submission)

    async with get_session() as session:
        await _resolve_submission_identity(session, submission, auth)
        _apply_github_attribution(submission)

        task, new_trials, is_append, experiment = await create_task_sweep_core(
            session,
            submission=submission,
            org_id=auth.org_id,
            default_environment=get_default_cloud_environment(),
            allowed_environments=ALLOWED_CLOUD_ENVIRONMENTS,
        )

        if not is_append:
            created_by_user_id = await _resolve_created_by_user_id(
                session, submission, auth
            )
            if created_by_user_id:
                task.created_by_user_id = created_by_user_id

            await _maybe_publish_experiment(session, task, submission, auth)

        elif experiment and submission.publish_experiment:
            await ensure_experiment_public(session, experiment)

        await session.commit()

        response_trials = new_trials if is_append else list(task.trials)
        provider_counts: Counter[str] = Counter(t.provider for t in response_trials)
        primary = experiment or (task.experiments[0] if task.experiments else None)
        resp_experiment_id = primary.id if primary else None
        resp_experiment_name = primary.name if primary else None

        return TaskResponse(
            id=task.id,
            name=task.name,
            status=task.status,
            priority=task.priority,
            trials_count=len(response_trials),
            providers=dict(provider_counts),
            experiment_id=resp_experiment_id,
            experiment_name=resp_experiment_name,
            created_at=task.created_at,
            new_trial_ids=[t.id for t in response_trials],
        )


# =============================================================================
# Task Listing and Retrieval
# =============================================================================


@router.get("/tasks", response_model=list[TaskStatusResponse])
async def list_tasks(
    request: Request,
    auth: Annotated[AuthContext, Depends(require_auth)],
    status: str | None = None,
    user: str | None = None,
    experiment_id: str | None = None,
    include_trials: bool = False,
    compact_trials: bool = False,
    compact_tasks: bool = False,
    include_queue_info: bool = True,
    include_worker_jobs: bool = True,
    limit: int = 100,
    offset: int = 0,
) -> list[TaskStatusResponse]:
    """List tasks for the authenticated organization.

    ``compact_tasks=true`` is a fast-path used by the experiment page
    first paint: it implies ``include_trials=false`` and skips the
    per-task ``visible_worker_jobs`` and ``effective_version_ids``
    lookups. The phase-2 batched fetch (``include_trials=true``) fills
    those columns in afterwards.
    """
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        connect_started_at = now()
        await session.connection()
        add_server_timing_metric(
            request,
            "db_connect",
            elapsed_ms(connect_started_at),
            "Tasks DB connect",
        )
        tasks = await list_tasks_core(
            session,
            status=status,
            user=user,
            experiment_id=experiment_id,
            include_trials=include_trials,
            compact_trials=compact_trials,
            compact_tasks=compact_tasks,
            include_queue_info=include_queue_info,
            include_worker_jobs=include_worker_jobs,
            limit=limit,
            offset=offset,
            org_id=auth.org_id,
            include_empty_rewards=True,
            record_timing=_make_timing_recorder(request),
        )
        return tasks


@router.get("/tasks/browse", response_model=TaskBrowseResponse)
async def browse_tasks(
    request: Request,
    auth: Annotated[AuthContext, Depends(require_auth)],
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    query: str | None = None,
) -> TaskBrowseResponse:
    """Browse latest task versions for the authenticated organization."""
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        connect_started_at = now()
        await session.connection()
        add_server_timing_metric(
            request,
            "db_connect",
            elapsed_ms(connect_started_at),
            "Browse DB connect",
        )
        return await browse_tasks_core(
            session,
            org_id=auth.org_id,
            limit=limit,
            offset=offset,
            query=query,
            record_timing=_make_timing_recorder(request),
        )


@router.get(
    "/experiments/{experiment_id}/share", response_model=ExperimentShareResponse
)
async def get_experiment_share(
    experiment_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> ExperimentShareResponse:
    """Get share status for an experiment."""
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        result = await session.execute(
            select(ExperimentModel).where(
                ExperimentModel.id == experiment_id,
                ExperimentModel.org_id == auth.org_id,
            )
        )
        experiment = result.scalar_one_or_none()
        if not experiment:
            raise HTTPException(status_code=404, detail="Experiment not found")

        return ExperimentShareResponse(
            name=experiment.name,
            is_public=bool(experiment.is_public),
            public_token=experiment.public_token,
        )


@router.patch(
    "/experiments/{experiment_id}",
    response_model=ExperimentUpdateResponse,
)
async def update_experiment(
    experiment_id: str,
    payload: ExperimentUpdateRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> ExperimentUpdateResponse:
    """Update experiment metadata."""
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Experiment name cannot be empty")

    async with get_session() as session:
        result = await session.execute(
            select(ExperimentModel).where(
                ExperimentModel.id == experiment_id,
                ExperimentModel.org_id == auth.org_id,
            )
        )
        experiment = result.scalar_one_or_none()
        if not experiment:
            raise HTTPException(status_code=404, detail="Experiment not found")

        experiment.name = name
        await session.commit()

        return ExperimentUpdateResponse(id=experiment.id, name=experiment.name)


@router.delete("/experiments/{experiment_id}")
async def delete_experiment(
    experiment_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    """Soft-delete an experiment and its experiment-scoped data.

    This tombstones the experiment plus its scoped trials and any tasks
    orphaned by removing the experiment membership. Artifacts remain in
    storage; the core path returns an empty ``s3_prefixes`` list so the
    API layer performs no hard-deletion follow-up.
    """
    async with get_session() as session:
        result = await delete_experiment_core(
            session, experiment_id=experiment_id, org_id=auth.org_id
        )
        await session.commit()
    invalidate_dashboard_cache(org_id=auth.org_id)

    return result


@router.post(
    "/experiments/{experiment_id}/publish",
    response_model=ExperimentShareResponse,
)
async def publish_experiment(
    experiment_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> ExperimentShareResponse:
    """Publish an experiment for public read-only access."""

    async with get_session() as session:
        result = await session.execute(
            select(ExperimentModel).where(
                ExperimentModel.id == experiment_id,
                ExperimentModel.org_id == auth.org_id,
            )
        )
        experiment = result.scalar_one_or_none()
        if not experiment:
            raise HTTPException(status_code=404, detail="Experiment not found")

        await ensure_experiment_public(session, experiment)
        await session.commit()

        return ExperimentShareResponse(
            name=experiment.name,
            is_public=True,
            public_token=experiment.public_token,
        )


@router.post(
    "/experiments/{experiment_id}/unpublish",
    response_model=ExperimentShareResponse,
)
async def unpublish_experiment(
    experiment_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> ExperimentShareResponse:
    """Unpublish an experiment (public link will stop working)."""

    async with get_session() as session:
        result = await session.execute(
            select(ExperimentModel).where(
                ExperimentModel.id == experiment_id,
                ExperimentModel.org_id == auth.org_id,
            )
        )
        experiment = result.scalar_one_or_none()
        if not experiment:
            raise HTTPException(status_code=404, detail="Experiment not found")

        experiment.is_public = False
        await session.commit()

        return ExperimentShareResponse(
            name=experiment.name,
            is_public=False,
            public_token=experiment.public_token,
        )


@router.post("/tasks/cancel")
async def cancel_tasks(
    payload: TaskBatchCancelRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Cancel in-flight runs for many tasks without deleting data."""
    auth.require_scope(APIKeyScope.TASKS)
    if not payload.task_ids:
        raise HTTPException(status_code=400, detail="Provide at least one task_id")

    async with get_session() as session:
        result = await cancel_tasks_runs(session, payload.task_ids, org_id=auth.org_id)
        if result.get("error") == "not_found":
            raise HTTPException(status_code=404, detail="No matching tasks found")
        await session.commit()

    modal_cancelled = await _cancel_modal_function_calls(
        result.get("modal_function_call_ids", [])
    )

    return {
        "status": "cancelled",
        "task_ids": result.get("task_ids", []),
        "not_found_task_ids": result.get("not_found_task_ids", []),
        "tasks_found": result.get("tasks_found", 0),
        "tasks_cancelled": result.get("tasks_cancelled", 0),
        "trials_cancelled": result.get("trials_cancelled", 0),
        "modal_calls_cancelled": modal_cancelled,
    }


@router.post("/tasks/{task_id}/analysis/retry")
async def retry_task_analysis(
    task_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Queue analysis jobs for every completed trial in a task."""
    auth.require_scope(APIKeyScope.TASKS)

    async with get_session() as session:
        return await rerun_task_analysis_core(
            session, task_id=task_id, org_id=auth.org_id
        )


@router.post("/tasks/{task_id}/verdict/retry")
async def retry_task_verdict(
    task_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Queue a fresh verdict job for a task whose analyses are complete."""
    auth.require_scope(APIKeyScope.TASKS)

    async with get_session() as session:
        return await rerun_task_verdict_core(
            session, task_id=task_id, org_id=auth.org_id
        )


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(
    task_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
    include_trials: bool = True,
) -> TaskStatusResponse:
    """Get task status with all trials for the authenticated organization."""
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        return await get_task_status_core(
            session,
            task_id=task_id,
            include_trials=include_trials,
            include_empty_rewards=True,
            org_id=auth.org_id,
        )


@router.get("/tasks/{task_id}/detail", response_model=TaskDetailResponse)
async def get_task_detail(
    task_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> TaskDetailResponse:
    """Task detail bundle: task + trials + per-version + cost rollups."""
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        return await get_task_detail_core(session, task_id=task_id, org_id=auth.org_id)


# =============================================================================
# Task Versions
# =============================================================================


@router.get("/tasks/{task_id}/versions", response_model=list[TaskVersionResponse])
async def list_task_versions(
    task_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> list[TaskVersionResponse]:
    """List all versions of a task, newest first."""
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        return await list_task_versions_core(
            session, task_id=task_id, org_id=auth.org_id
        )


@router.get("/tasks/{task_id}/versions/{version}", response_model=TaskVersionResponse)
async def get_task_version(
    task_id: str,
    version: int,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> TaskVersionResponse:
    """Get a specific version of a task."""
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        return await get_task_version_core(
            session, task_id=task_id, version=version, org_id=auth.org_id
        )


# =============================================================================
# Task Files (S3 Storage)
# =============================================================================


def _build_task_file_etag(archive_etag: str, file_path: str) -> str:
    """Compose an RFC 7232 weak-etag for a task-archive-served file.

    S3's ``head_object`` returns the ``ETag`` already wrapped in double
    quotes (e.g. ``'"abc123"'``); embedding that verbatim inside
    ``W/"..."`` would emit a malformed header that browsers silently
    ignore, which would defeat the whole HTTP-cache fast path. Strip
    any leading/trailing quotes before composing the wire form.
    """
    normalized = archive_etag.strip().strip('"')
    return f'W/"{normalized}:{file_path}"'


@router.get("/tasks/{task_id}/files")
async def list_task_files(
    task_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
    prefix: str | None = Query(None),
    recursive: bool = Query(True),
    limit: int = Query(1000, ge=1, le=1000),
    cursor: str | None = Query(None),
    presign: bool = Query(
        True, description="Include presigned URLs for direct S3 access"
    ),
    version: int | None = Query(None, description="Task version number"),
) -> dict:
    """List all files in a task's S3 directory.

    When presign=True (default), includes presigned URLs for each file,
    allowing clients to fetch content directly from S3 without additional API calls.
    """
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        task = await get_task_for_org_core(
            session,
            task_id=task_id,
            org_id=auth.org_id,
            load_current_version=True,
        )
        if version is None and task.current_version:
            version = task.current_version.version

    return await list_task_files_s3(
        task_id=task_id,
        prefix=prefix,
        recursive=recursive,
        limit=limit,
        cursor=cursor,
        presign=presign,
        version=version,
    )


@router.get("/tasks/{task_id}/files/{file_path:path}")
async def get_task_file_content(
    task_id: str,
    file_path: str,
    request: Request,
    response: Response,
    auth: Annotated[AuthContext, Depends(require_auth)],
    presign: bool = Query(False),
    version: int | None = Query(None, description="Task version number"),
):
    """Get content of a specific task file from S3.

    When the underlying source is a pinned task archive (immutable at a
    given version) the response carries ``ETag`` + ``Cache-Control``
    headers and honors ``If-None-Match`` with a ``304``, so the browser's
    HTTP cache covers repeated clicks on the same file.
    """
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        task = await get_task_for_org_core(
            session,
            task_id=task_id,
            org_id=auth.org_id,
            load_current_version=True,
        )
        if version is None and task.current_version:
            version = task.current_version.version

    result = await get_task_file_content_s3(
        task_id=task_id,
        file_path=file_path,
        presign=presign,
        version=version,
    )

    archive_etag = result.get("archive_etag")
    if archive_etag and version is not None:
        etag_value = _build_task_file_etag(str(archive_etag), file_path)
        if_none_match = request.headers.get("if-none-match")
        if if_none_match and etag_value in {
            h.strip() for h in if_none_match.split(",")
        }:
            return Response(
                status_code=304,
                headers={
                    "ETag": etag_value,
                    "Cache-Control": "private, max-age=86400, immutable",
                },
            )
        response.headers["ETag"] = etag_value
        response.headers["Cache-Control"] = "private, max-age=86400, immutable"

    return result

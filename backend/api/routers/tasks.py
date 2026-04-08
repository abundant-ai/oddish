from __future__ import annotations

import asyncio
from collections import Counter
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from harbor.models.environment_type import EnvironmentType
from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_policy import (
    ALLOWED_CLOUD_ENVIRONMENTS,
    get_default_cloud_environment,
)
from oddish.api.endpoints import (
    browse_tasks_core,
    create_task_sweep_core,
    delete_experiment_core,
    delete_task_core,
    get_task_for_org_core,
    get_task_status_core,
    get_task_version_core,
    list_tasks_core,
    list_task_versions_core,
    rerun_task_analysis_core,
    rerun_task_verdict_core,
)
from oddish.api.public_helpers import (
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
from oddish.api.tasks import handle_task_upload, resolve_task_storage
from oddish.api.sweeps import (
    build_task_submission_from_sweep,
    build_trial_specs_from_sweep,
    validate_sweep_submission,
)
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TaskStatus,
    TrialModel,
    get_session,
)
from oddish.db.storage import collect_s3_prefixes_for_deletion, delete_s3_prefixes
from oddish.queue import (
    append_trials_to_task,
    cancel_tasks_runs,
    create_task,
)
from oddish.schemas import (
    TaskBrowseResponse,
    TaskBatchCancelRequest,
    TaskResponse,
    TaskStatusResponse,
    TaskSweepSubmission,
    TaskVersionResponse,
    UploadResponse,
)

router = APIRouter(tags=["Tasks"])
logger = logging.getLogger(__name__)
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


def _compact_trial_payloads(
    tasks: list[TaskStatusResponse],
) -> list[TaskStatusResponse]:
    """Trim heavy per-trial fields for list/table views."""
    for task in tasks:
        if not task.trials:
            continue
        for trial in task.trials:
            # These fields can be large and are not required for matrix rendering.
            trial.result = None
            trial.input_tokens = None
            trial.cache_tokens = None
            trial.output_tokens = None
            trial.cost_usd = None
            trial.phase_timing = None

            # Keep only lightweight analysis summary used by the UI.
            if isinstance(trial.analysis, dict):
                trial.analysis = {
                    "classification": trial.analysis.get("classification"),
                    "subtype": trial.analysis.get("subtype"),
                }
    return tasks


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

    experiment = await session.get(ExperimentModel, task.experiment_id)
    if experiment:
        await ensure_experiment_public(session, experiment)


# =============================================================================
# Task Upload and Creation
# =============================================================================


@router.post("/tasks/upload", response_model=UploadResponse)
async def upload_task(
    auth: Annotated[AuthContext, Depends(require_auth)],
    file: UploadFile = File(...),
    content_hash: str | None = None,
    message: str | None = None,
) -> UploadResponse:
    """Upload a task directory (as tarball) to storage.

    Automatically detects existing tasks by name within the org and creates
    a new version when content has changed.
    """
    auth.require_scope(APIKeyScope.TASKS)

    return await handle_task_upload(
        file,
        org_id=auth.org_id,
        content_hash=content_hash,
        message=message,
        created_by_user_id=auth.user_id,
    )


@router.post("/tasks/sweep", response_model=TaskResponse)
async def create_task_sweep(
    submission: TaskSweepSubmission,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> TaskResponse:
    """Submit a task sweep - expands a task_id into many trials."""
    auth.require_scope(APIKeyScope.TASKS)

    from oddish.api.sweeps import validate_sweep_submission
    validate_sweep_submission(submission)
    _apply_github_attribution(submission)

    async with get_session() as session:
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

        provider_counts: Counter[str] = Counter(
            t.provider for t in (new_trials if is_append else task.trials)
        )
        resp_experiment_id = experiment.id if experiment else task.experiment_id
        resp_experiment_name = getattr(experiment, "name", None) if experiment else getattr(task.experiment, "name", None)
        
        return TaskResponse(
            id=task.id,
            name=task.name,
            status=task.status,
            priority=task.priority,
            trials_count=len(new_trials) if is_append else len(task.trials),
            providers=dict(provider_counts),
            experiment_id=resp_experiment_id,
            experiment_name=resp_experiment_name,
            created_at=task.created_at,
        )


# =============================================================================
# Task Listing and Retrieval
# =============================================================================


@router.get("/tasks", response_model=list[TaskStatusResponse])
async def list_tasks(
    auth: Annotated[AuthContext, Depends(require_auth)],
    status: str | None = None,
    user: str | None = None,
    experiment_id: str | None = None,
    include_trials: bool = False,
    compact_trials: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[TaskStatusResponse]:
    """List tasks for the authenticated organization."""
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        tasks = await list_tasks_core(
            session,
            status=status,
            user=user,
            experiment_id=experiment_id,
            include_trials=include_trials,
            compact_trials=compact_trials,
            limit=limit,
            offset=offset,
            org_id=auth.org_id,
            include_empty_rewards=True,
        )
        return tasks


@router.get("/tasks/browse", response_model=TaskBrowseResponse)
async def browse_tasks(
    auth: Annotated[AuthContext, Depends(require_auth)],
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    query: str | None = None,
) -> TaskBrowseResponse:
    """Browse latest task versions for the authenticated organization."""
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        return await browse_tasks_core(
            session,
            org_id=auth.org_id,
            limit=limit,
            offset=offset,
            query=query,
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


@router.delete("/experiments/{experiment_id}")
async def delete_experiment(
    experiment_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    """Delete an experiment and all associated tasks/trials."""

    async with get_session() as session:
        result = await delete_experiment_core(session, experiment_id=experiment_id, org_id=auth.org_id)
        await session.commit()

    if result.get("s3_prefixes"):
        try:
            await delete_s3_prefixes(result["s3_prefixes"])
        except Exception:
            logger.exception(
                "Failed to delete S3 artifacts for experiment %s", experiment_id
            )

    return {
        "status": "success",
        "message": "Experiment deleted",
        "deleted": result["deleted"],
    }


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


@router.delete("/tasks/{task_id}")
async def delete_task(
    task_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    """Delete a task and its trials."""

    async with get_session() as session:
        result = await delete_task_core(session, task_id=task_id, org_id=auth.org_id)
        await session.commit()

    if result.get("s3_prefixes"):
        try:
            await delete_s3_prefixes(result["s3_prefixes"])
        except Exception:
            logger.exception("Failed to delete S3 artifacts for task %s", task_id)

    return {"status": "success", "deleted": result["deleted"]}


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
        task = await get_task_for_org_core(session, task_id=task_id, org_id=auth.org_id)
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
    auth: Annotated[AuthContext, Depends(require_auth)],
    presign: bool = Query(False),
    version: int | None = Query(None, description="Task version number"),
) -> dict:
    """Get content of a specific task file from S3."""
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        task = await get_task_for_org_core(session, task_id=task_id, org_id=auth.org_id)
        if version is None and task.current_version:
            version = task.current_version.version

    return await get_task_file_content_s3(
        task_id=task_id,
        file_path=file_path,
        presign=presign,
        version=version,
    )

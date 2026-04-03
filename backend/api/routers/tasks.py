from __future__ import annotations

import asyncio
from collections import Counter
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
    get_task_for_org_core,
    get_task_status_core,
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
from oddish.api.endpoints import list_tasks_core
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TaskStatus,
    TrialModel,
    get_session,
)
from oddish.queue import (
    append_trials_to_task,
    cancel_tasks_runs,
    create_task,
)
from oddish.schemas import (
    TaskBatchCancelRequest,
    TaskResponse,
    TaskStatusResponse,
    TaskSweepSubmission,
    UploadResponse,
)

router = APIRouter(tags=["Tasks"])
MODAL_CANCEL_BATCH_SIZE = 32


async def _cancel_modal_function_calls(modal_fc_ids: list[str]) -> int:
    if not modal_fc_ids:
        return 0

    import modal

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
) -> UploadResponse:
    """Upload a task directory (as tarball) to storage."""
    auth.require_scope(APIKeyScope.TASKS)

    return await handle_task_upload(file)


@router.post("/tasks/sweep", response_model=TaskResponse)
async def create_task_sweep(
    submission: TaskSweepSubmission,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> TaskResponse:
    """Submit a task sweep - expands a task_id into many trials."""
    auth.require_scope(APIKeyScope.TASKS)

    validate_sweep_submission(submission)
    _apply_github_attribution(submission)

    async with get_session() as session:
        if submission.append_to_task:
            if submission.experiment_id:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Cannot set experiment_id when appending to an existing task"
                    ),
                )

            task = await get_task_for_org_core(
                session, task_id=submission.task_id, org_id=auth.org_id
            )
            if task.status in (TaskStatus.ANALYZING, TaskStatus.VERDICT_PENDING):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Cannot append trials while task analysis or verdict "
                        "is in progress"
                    ),
                )
            if submission.run_analysis and not task.run_analysis:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Cannot enable run_analysis when appending to a task "
                        "that was created without it"
                    ),
                )

            existing_env_result = await session.execute(
                select(TrialModel.environment)
                .where(
                    TrialModel.task_id == task.id,
                    TrialModel.environment.is_not(None),
                )
                .order_by(TrialModel.created_at.asc(), TrialModel.id.asc())
                .limit(1)
            )
            existing_environment = existing_env_result.scalar_one_or_none()
            default_environment = (
                EnvironmentType(existing_environment)
                if existing_environment
                else get_default_cloud_environment()
            )

            trials = build_trial_specs_from_sweep(
                submission,
                default_environment=default_environment,
                allowed_environments=ALLOWED_CLOUD_ENVIRONMENTS,
            )
            append_submission = submission.model_copy(
                update={
                    "name": task.name,
                    "priority": task.priority,
                    "experiment_id": task.experiment_id,
                    "tags": task.tags or {},
                    "run_analysis": task.run_analysis,
                    "user": task.user,
                }
            )
            expanded = build_task_submission_from_sweep(
                append_submission, task_path=task.task_path, trials=trials
            )
            new_trials = await append_trials_to_task(
                session, task=task, submission=expanded
            )
            await _maybe_publish_experiment(session, task, submission, auth)
            await session.commit()

            provider_counts: Counter[str] = Counter(t.provider for t in new_trials)
            return TaskResponse(
                id=task.id,
                name=task.name,
                status=task.status,
                priority=task.priority,
                trials_count=len(new_trials),
                providers=dict(provider_counts),
                created_at=task.created_at,
            )

        task_path, task_s3_key = await resolve_task_storage(submission.task_id)
        trials = build_trial_specs_from_sweep(
            submission,
            default_environment=get_default_cloud_environment(),
            allowed_environments=ALLOWED_CLOUD_ENVIRONMENTS,
        )
        expanded = build_task_submission_from_sweep(
            submission, task_path=task_path, trials=trials
        )

        # Pass org_id to create_task - it propagates to experiment and trials
        try:
            task = await create_task(
                session, expanded, task_id=submission.task_id, org_id=auth.org_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        created_by_user_id = await _resolve_created_by_user_id(
            session, submission, auth
        )
        if created_by_user_id:
            task.created_by_user_id = created_by_user_id

        await _maybe_publish_experiment(session, task, submission, auth)

        if task_s3_key:
            task.task_s3_key = task_s3_key
        await session.commit()

        provider_counts: Counter[str] = Counter(t.provider for t in task.trials)
        return TaskResponse(
            id=task.id,
            name=task.name,
            status=task.status,
            priority=task.priority,
            trials_count=len(task.trials),
            providers=dict(provider_counts),
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
        result = await session.execute(
            select(ExperimentModel).where(
                ExperimentModel.id == experiment_id,
                ExperimentModel.org_id == auth.org_id,
            )
        )
        experiment = result.scalar_one_or_none()
        if not experiment:
            raise HTTPException(status_code=404, detail="Experiment not found")

        task_ids_result = await session.execute(
            select(TaskModel.id)
            .where(TaskModel.experiment_id == experiment.id)
            .where(TaskModel.org_id == auth.org_id)
        )
        task_ids = [row[0] for row in task_ids_result.all()]
        trial_ids: list[str] = []
        if task_ids:
            trial_ids_result = await session.execute(
                select(TrialModel.id).where(TrialModel.task_id.in_(task_ids))
            )
            trial_ids = [row[0] for row in trial_ids_result.all()]

        trials_result = await session.execute(
            delete(TrialModel).where(TrialModel.task_id.in_(task_ids))
        )
        if not isinstance(trials_result, CursorResult):
            raise TypeError("Expected CursorResult for trial delete")

        tasks_result = await session.execute(
            delete(TaskModel)
            .where(TaskModel.experiment_id == experiment.id)
            .where(TaskModel.org_id == auth.org_id)
        )
        if not isinstance(tasks_result, CursorResult):
            raise TypeError("Expected CursorResult for task delete")

        experiments_result = await session.execute(
            delete(ExperimentModel)
            .where(ExperimentModel.id == experiment.id)
            .where(ExperimentModel.org_id == auth.org_id)
        )
        if not isinstance(experiments_result, CursorResult):
            raise TypeError("Expected CursorResult for experiment delete")

        await session.commit()

    deleted_trials = int(trials_result.rowcount or 0)
    deleted_tasks = int(tasks_result.rowcount or 0)
    deleted_experiments = int(experiments_result.rowcount or 0)

    return {
        "status": "success",
        "message": "Experiment deleted",
        "deleted": {
            "trials": deleted_trials,
            "tasks": deleted_tasks,
            "experiments": deleted_experiments,
        },
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
        task = await get_task_for_org_core(session, task_id=task_id, org_id=auth.org_id)
        trial_ids_result = await session.execute(
            select(TrialModel.id).where(TrialModel.task_id == task.id)
        )
        trial_ids = [row[0] for row in trial_ids_result.all()]

        await session.delete(task)
        await session.commit()

    return {"status": "success", "deleted": {"task_id": task_id}}


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
) -> dict:
    """List all files in a task's S3 directory.

    When presign=True (default), includes presigned URLs for each file,
    allowing clients to fetch content directly from S3 without additional API calls.
    """
    auth.require_scope(APIKeyScope.READ)

    # Verify task belongs to user's org
    async with get_session() as session:
        await get_task_for_org_core(session, task_id=task_id, org_id=auth.org_id)

    return await list_task_files_s3(
        task_id=task_id,
        prefix=prefix,
        recursive=recursive,
        limit=limit,
        cursor=cursor,
        presign=presign,
    )


@router.get("/tasks/{task_id}/files/{file_path:path}")
async def get_task_file_content(
    task_id: str,
    file_path: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
    presign: bool = Query(False),
) -> dict:
    """Get content of a specific task file from S3."""
    auth.require_scope(APIKeyScope.READ)

    # Verify task belongs to user's org
    async with get_session() as session:
        await get_task_for_org_core(session, task_id=task_id, org_id=auth.org_id)

    return await get_task_file_content_s3(
        task_id=task_id,
        file_path=file_path,
        presign=presign,
    )

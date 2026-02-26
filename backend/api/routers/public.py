from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from oddish.api.helpers import build_task_status_response
from oddish.api.trial_io import (
    read_trial_agent_file,
    read_trial_logs,
    read_trial_logs_structured,
    read_trial_result,
    read_trial_trajectory,
)
from api.routers._helpers import (
    get_public_experiment,
    get_public_task,
    get_public_trial,
    get_task_file_content_s3,
    get_task_status_counts,
    get_trial_file_content_s3,
    list_task_files_s3,
    list_task_trials_for_task,
    list_trial_files_s3,
)
from api.schemas import PublicExperimentListItem, PublicExperimentResponse
from oddish.db import ExperimentModel, TaskModel, get_session
from oddish.schemas import TaskStatusResponse, TrialResponse

router = APIRouter(tags=["Public"])


@router.get(
    "/public/experiments",
    response_model=list[PublicExperimentListItem],
)
async def list_public_experiments(
    limit: int = 100,
    offset: int = 0,
) -> list[PublicExperimentListItem]:
    """List all public experiments for dataset browsing."""
    async with get_session() as session:
        query = (
            select(
                ExperimentModel.id,
                ExperimentModel.name,
                ExperimentModel.public_token,
                ExperimentModel.created_at,
                func.count(TaskModel.id).label("task_count"),
            )
            .outerjoin(TaskModel, TaskModel.experiment_id == ExperimentModel.id)
            .where(ExperimentModel.is_public == True)  # noqa: E712
            .where(ExperimentModel.public_token.is_not(None))
            .group_by(
                ExperimentModel.id,
                ExperimentModel.name,
                ExperimentModel.public_token,
                ExperimentModel.created_at,
            )
            .order_by(ExperimentModel.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await session.execute(query)
        rows = result.all()

        return [
            PublicExperimentListItem(
                id=row.id,
                name=row.name,
                public_token=row.public_token,
                task_count=int(row.task_count or 0),
                created_at=row.created_at.isoformat(),
            )
            for row in rows
            if row.public_token
        ]


@router.get(
    "/public/experiments/{public_token}", response_model=PublicExperimentResponse
)
async def get_public_experiment_info(public_token: str) -> PublicExperimentResponse:
    """Get public experiment metadata by share token."""
    async with get_session() as session:
        experiment = await get_public_experiment(session, public_token)
        if not experiment:
            raise HTTPException(status_code=404, detail="Experiment not found")

        return PublicExperimentResponse(
            name=experiment.name,
            public_token=experiment.public_token or public_token,
        )


@router.get(
    "/public/experiments/{public_token}/tasks", response_model=list[TaskStatusResponse]
)
async def list_public_experiment_tasks(
    public_token: str,
    limit: int = 200,
    offset: int = 0,
) -> list[TaskStatusResponse]:
    """List tasks (with trials) for a public experiment."""
    async with get_session() as session:
        experiment = await get_public_experiment(session, public_token)
        if not experiment:
            raise HTTPException(status_code=404, detail="Experiment not found")

        query = (
            select(TaskModel)
            .options(selectinload(TaskModel.trials), selectinload(TaskModel.experiment))
            .join(ExperimentModel)
            .where(ExperimentModel.public_token == public_token)
            .where(ExperimentModel.is_public == True)  # noqa: E712
            .order_by(TaskModel.created_at.desc())
            .limit(limit)
            .offset(offset)
        )

        result = await session.execute(query)
        tasks = result.scalars().all()
        return [build_task_status_response(task) for task in tasks]


@router.get("/public/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_public_task_status(
    task_id: str,
    include_trials: bool = True,
) -> TaskStatusResponse:
    """Get task status for a public experiment."""
    async with get_session() as session:
        if include_trials:
            task = await get_public_task(session, task_id)
            if not task:
                raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
            return build_task_status_response(task)

        return await get_task_status_counts(
            session,
            task_id,
            filters=[ExperimentModel.is_public == True],  # noqa: E712
            join_experiment=True,
        )


@router.get("/public/tasks/{task_id}/trials", response_model=list[TrialResponse])
async def list_public_task_trials(task_id: str) -> list[TrialResponse]:
    """List all trials for a public task."""
    async with get_session() as session:
        task = await get_public_task(session, task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        return await list_task_trials_for_task(session, task_id)


@router.get("/public/trials/{trial_id}/logs")
async def get_public_trial_logs(trial_id: str) -> dict:
    """Get logs for a public trial."""
    async with get_session() as session:
        trial = await get_public_trial(session, trial_id)
        if not trial:
            raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")
        return await read_trial_logs(trial)


@router.get("/public/trials/{trial_id}/logs/structured")
async def get_public_trial_logs_structured(trial_id: str) -> dict:
    """Get structured logs for a public trial."""
    async with get_session() as session:
        trial = await get_public_trial(session, trial_id)
        if not trial:
            raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")

        return await read_trial_logs_structured(trial)


@router.get("/public/trials/{trial_id}/trajectory")
async def get_public_trial_trajectory(trial_id: str) -> dict | None:
    """Get ATIF trajectory.json for a public trial."""
    async with get_session() as session:
        trial = await get_public_trial(session, trial_id)
        if not trial:
            raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")

        return await read_trial_trajectory(trial)


@router.get("/public/trials/{trial_id}/files")
async def list_public_trial_files(trial_id: str) -> dict:
    """List all files in a public trial's S3 directory."""
    async with get_session() as session:
        trial = await get_public_trial(session, trial_id)
        if not trial:
            raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")

        return await list_trial_files_s3(trial)


@router.get("/public/trials/{trial_id}/files/{file_path:path}")
async def get_public_trial_file(trial_id: str, file_path: str) -> Response:
    """Get a file from a public trial's S3 directory."""
    async with get_session() as session:
        trial = await get_public_trial(session, trial_id)
        if not trial:
            raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")

        try:
            content, media_type = await get_trial_file_content_s3(trial, file_path)
            return Response(content=content, media_type=media_type)
        except HTTPException:
            pass
        content, media_type = await read_trial_agent_file(trial, file_path)
        return Response(content=content, media_type=media_type)


@router.get("/public/trials/{trial_id}/result")
async def get_public_trial_result(trial_id: str) -> dict:
    """Get result.json for a public trial."""
    async with get_session() as session:
        trial = await get_public_trial(session, trial_id)
        if not trial:
            raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")

        return await read_trial_result(trial)


@router.get("/public/tasks/{task_id}/files")
async def list_public_task_files(
    task_id: str,
    prefix: str | None = Query(None),
    recursive: bool = Query(True),
    limit: int = Query(1000, ge=1, le=1000),
    cursor: str | None = Query(None),
    presign: bool = Query(
        True, description="Include presigned URLs for direct S3 access"
    ),
) -> dict:
    """List all files in a public task's S3 directory."""
    # Verify task belongs to a public experiment
    async with get_session() as session:
        task = await get_public_task(session, task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    return await list_task_files_s3(
        task_id=task_id,
        prefix=prefix,
        recursive=recursive,
        limit=limit,
        cursor=cursor,
        presign=presign,
    )


@router.get("/public/tasks/{task_id}/files/{file_path:path}")
async def get_public_task_file_content(
    task_id: str,
    file_path: str,
    presign: bool = Query(False),
) -> dict:
    """Get content of a specific public task file from S3."""
    # Verify task belongs to a public experiment
    async with get_session() as session:
        task = await get_public_task(session, task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    return await get_task_file_content_s3(
        task_id=task_id,
        file_path=file_path,
        presign=presign,
    )

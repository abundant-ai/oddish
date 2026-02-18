from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from oddish.api.helpers import (
    build_task_status_response,
    build_task_status_responses_from_counts,
    build_trial_response,
)
from oddish.api.trial_io import (
    read_trial_logs,
    read_trial_logs_structured,
    read_trial_result,
    read_trial_trajectory,
)
from oddish.db import TaskModel, TrialModel
from oddish.schemas import TaskStatusResponse, TrialResponse


async def get_task_for_org_core(
    session: AsyncSession,
    *,
    task_id: str,
    org_id: str | None = None,
) -> TaskModel:
    """Fetch a task by ID with optional org scoping."""
    query = select(TaskModel).where(TaskModel.id == task_id)
    if org_id is not None:
        query = query.where(TaskModel.org_id == org_id)
    result = await session.execute(query)
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return task


async def list_tasks_core(
    session: AsyncSession,
    *,
    status: str | None = None,
    user: str | None = None,
    experiment_id: str | None = None,
    include_trials: bool = True,
    limit: int = 100,
    offset: int = 0,
    org_id: str | None = None,
    include_empty_rewards: bool = True,
) -> list[TaskStatusResponse]:
    """List tasks with optional filters and aggregated trial stats."""
    query = select(TaskModel).order_by(TaskModel.created_at.desc())
    if include_trials:
        query = query.options(
            selectinload(TaskModel.trials), selectinload(TaskModel.experiment)
        )
    else:
        query = query.options(selectinload(TaskModel.experiment))

    if org_id is not None:
        query = query.where(TaskModel.org_id == org_id)
    if status:
        query = query.where(TaskModel.status == status)
    if user:
        query = query.where(TaskModel.user == user)
    if experiment_id:
        query = query.where(TaskModel.experiment_id == experiment_id)

    query = query.limit(limit).offset(offset)
    result = await session.execute(query)
    tasks = result.scalars().all()

    if include_trials:
        return [
            build_task_status_response(
                task, include_empty_rewards=include_empty_rewards
            )
            for task in tasks
        ]

    return await build_task_status_responses_from_counts(
        session,
        tasks=tasks,
        include_empty_rewards=include_empty_rewards,
    )


async def get_task_status_core(
    session: AsyncSession,
    *,
    task_id: str,
    include_trials: bool = True,
    include_empty_rewards: bool = True,
    org_id: str | None = None,
) -> TaskStatusResponse:
    """Get task status with optional org scoping."""
    query = select(TaskModel).options(selectinload(TaskModel.experiment))
    if include_trials:
        query = query.options(selectinload(TaskModel.trials))
    query = query.where(TaskModel.id == task_id)
    if org_id is not None:
        query = query.where(TaskModel.org_id == org_id)
    result = await session.execute(query)
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    if include_trials:
        return build_task_status_response(
            task, include_empty_rewards=include_empty_rewards
        )

    return (
        await build_task_status_responses_from_counts(
            session, tasks=[task], include_empty_rewards=include_empty_rewards
        )
    )[0]


async def get_trial_by_index_core(
    session: AsyncSession,
    *,
    task_id: str,
    index: int,
    org_id: str | None = None,
) -> TrialResponse:
    """Get trial response by 0-based index with optional org scoping."""
    trial_id = f"{task_id}-{index}"
    result = await session.execute(
        select(TrialModel, TaskModel.task_path, TaskModel.org_id)
        .join(TaskModel, TaskModel.id == TrialModel.task_id)
        .where(TrialModel.id == trial_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")

    trial, task_path, task_org_id = row
    if org_id is not None and task_org_id != org_id:
        raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")

    return build_trial_response(trial, task_path)


async def get_trial_for_org_core(
    session: AsyncSession,
    *,
    trial_id: str,
    org_id: str | None = None,
) -> TrialModel:
    """Fetch a trial with optional org scoping via its task."""
    result = await session.execute(
        select(TrialModel, TaskModel.org_id)
        .join(TaskModel, TaskModel.id == TrialModel.task_id)
        .where(TrialModel.id == trial_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")

    trial, task_org_id = row
    if org_id is not None and task_org_id != org_id:
        raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")

    return trial


async def get_trial_logs_core(
    session: AsyncSession,
    *,
    trial_id: str,
    org_id: str | None = None,
) -> dict:
    """Get trial logs with optional org scoping."""
    trial = await get_trial_for_org_core(session, trial_id=trial_id, org_id=org_id)
    return await read_trial_logs(trial)


async def get_trial_logs_structured_core(
    session: AsyncSession,
    *,
    trial_id: str,
    org_id: str | None = None,
) -> dict:
    """Get structured trial logs with optional org scoping."""
    trial = await get_trial_for_org_core(session, trial_id=trial_id, org_id=org_id)
    return await read_trial_logs_structured(trial)


async def get_trial_trajectory_core(
    session: AsyncSession,
    *,
    trial_id: str,
    org_id: str | None = None,
) -> dict | None:
    """Get trial trajectory with optional org scoping."""
    trial = await get_trial_for_org_core(session, trial_id=trial_id, org_id=org_id)
    return await read_trial_trajectory(trial)


async def get_trial_result_core(
    session: AsyncSession,
    *,
    trial_id: str,
    org_id: str | None = None,
) -> dict:
    """Get trial result with optional org scoping."""
    trial = await get_trial_for_org_core(session, trial_id=trial_id, org_id=org_id)
    return await read_trial_result(trial)

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only, selectinload

from oddish.api.helpers import (
    build_task_status_response_compact,
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
from oddish.config import settings
from oddish.db import (
    ExperimentModel,
    Priority,
    TaskModel,
    TaskStatus,
    TrialModel,
    TrialStatus,
)
from oddish.queue import enqueue_trial
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
    compact_trials: bool = False,
    limit: int = 100,
    offset: int = 0,
    org_id: str | None = None,
    include_empty_rewards: bool = True,
) -> list[TaskStatusResponse]:
    """List tasks with optional filters and aggregated trial stats."""
    query = select(TaskModel).order_by(TaskModel.created_at.desc())
    if include_trials:
        trials_loader = selectinload(TaskModel.trials)
        experiment_loader = selectinload(TaskModel.experiment)
        if compact_trials:
            trials_loader = trials_loader.load_only(
                TrialModel.id,
                TrialModel.name,
                TrialModel.task_id,
                TrialModel.agent,
                TrialModel.provider,
                TrialModel.queue_key,
                TrialModel.model,
                TrialModel.status,
                TrialModel.attempts,
                TrialModel.max_attempts,
                TrialModel.harbor_stage,
                TrialModel.reward,
                TrialModel.error_message,
                TrialModel.has_trajectory,
                TrialModel.analysis_status,
                TrialModel.analysis,
                TrialModel.created_at,
                TrialModel.started_at,
                TrialModel.finished_at,
            )
            experiment_loader = experiment_loader.load_only(
                ExperimentModel.id,
                ExperimentModel.name,
                ExperimentModel.is_public,
            )
            query = query.options(
                load_only(
                    TaskModel.id,
                    TaskModel.name,
                    TaskModel.status,
                    TaskModel.priority,
                    TaskModel.user,
                    TaskModel.tags,
                    TaskModel.task_path,
                    TaskModel.experiment_id,
                    TaskModel.run_analysis,
                    TaskModel.verdict_status,
                    TaskModel.verdict,
                    TaskModel.verdict_error,
                    TaskModel.created_at,
                    TaskModel.started_at,
                    TaskModel.finished_at,
                ),
                trials_loader,
                experiment_loader,
            )
        else:
            query = query.options(trials_loader, experiment_loader)
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
        if compact_trials:
            return [
                build_task_status_response_compact(
                    task, include_empty_rewards=include_empty_rewards
                )
                for task in tasks
            ]
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
    result = await session.execute(select(TrialModel).where(TrialModel.id == trial_id))
    trial = result.scalar_one_or_none()
    if not trial:
        raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")

    if org_id is not None:
        if trial.org_id is not None:
            if trial.org_id != org_id:
                raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")
        else:
            # Fallback for legacy rows where trial.org_id is not populated.
            task_org_result = await session.execute(
                select(TaskModel.org_id).where(TaskModel.id == trial.task_id)
            )
            task_org_id = task_org_result.scalar_one_or_none()
            if task_org_id != org_id:
                raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")

    return trial


async def retry_trial_core(
    session: AsyncSession,
    *,
    trial_id: str,
    org_id: str | None = None,
) -> dict[str, str]:
    """Reset and requeue a trial for another attempt."""
    trial = await get_trial_for_org_core(session, trial_id=trial_id, org_id=org_id)
    task = await session.get(TaskModel, trial.task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")

    # Allow retrying terminal states OR stuck trials.
    # A trial is "stuck" if running/retrying with error or completed harbor stage.
    terminal_states = {TrialStatus.FAILED, TrialStatus.SUCCESS}
    is_stuck = trial.status in {TrialStatus.RUNNING, TrialStatus.RETRYING} and (
        trial.error_message or trial.harbor_stage == "completed"
    )
    if trial.status not in terminal_states and not is_stuck:
        raise HTTPException(
            status_code=400,
            detail=f"Can only retry completed, failed, or stuck trials (current: {trial.status.value})",
        )

    trial.status = TrialStatus.QUEUED
    trial.error_message = None
    trial.reward = None
    trial.result = None
    trial.started_at = None
    trial.finished_at = None
    trial.harbor_stage = None
    trial.harbor_result_path = None
    trial.trial_s3_key = None
    trial.attempts = 0
    # Clear idempotency key so worker can process this retry.
    trial.idempotency_key = None

    pgq_priority = 1000 if task.priority == Priority.HIGH else 0
    queue_key = trial.queue_key or settings.get_queue_key_for_trial(trial.agent, trial.model)
    await enqueue_trial(session, trial_id, queue_key, priority=pgq_priority)

    # Move completed tasks back to running once a trial is requeued.
    if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
        task.status = TaskStatus.RUNNING
        task.finished_at = None

    await session.commit()
    return {"status": "queued", "trial_id": trial_id}


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

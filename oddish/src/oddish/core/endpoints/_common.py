from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from oddish.db import ExperimentModel, TaskModel, TrialModel

USER_CANCELLED_MESSAGE = "Cancelled by user"
_ACTIVE_WORKER_JOB_STATUSES_SQL = "'QUEUED', 'RETRYING', 'RUNNING', 'BLOCKED'"


async def _primary_experiment_for_task_model(
    task: TaskModel,
) -> ExperimentModel | None:
    """Pick the first linked experiment as the task's "primary" experiment.

    Used by response builders and sweep/append plumbing that still need a
    single experiment context when the task participates in several.

    Uses ``awaitable_attrs`` so the ``experiments`` relationship is safe to
    access even when it hasn't been eagerly loaded on ``task``.
    """
    experiments = list(await task.awaitable_attrs.experiments or [])
    return experiments[0] if experiments else None


async def get_task_for_org_core(
    session: AsyncSession,
    *,
    task_id: str,
    org_id: str | None = None,
    load_current_version: bool = False,
) -> TaskModel:
    """Fetch a task by ID with optional org scoping.

    Pass ``load_current_version=True`` from callers that read
    ``task.current_version.version`` (e.g. the file-serving routes).
    Default is False so the common path doesn't pay for an extra
    ``task_versions`` round trip it never consumes -- ``current_version``
    is now lazy on TaskModel itself.
    """
    query = select(TaskModel).where(TaskModel.id == task_id)
    if load_current_version:
        query = query.options(selectinload(TaskModel.current_version))
    if org_id is not None:
        query = query.where(TaskModel.org_id == org_id)
    result = await session.execute(query)
    task: TaskModel | None = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return task


async def get_trial_for_org_core(
    session: AsyncSession,
    *,
    trial_id: str,
    org_id: str | None = None,
) -> TrialModel:
    """Fetch a trial with optional org scoping via its task."""
    result = await session.execute(select(TrialModel).where(TrialModel.id == trial_id))
    trial: TrialModel | None = result.scalar_one_or_none()
    if not trial:
        raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")

    if org_id is not None:
        if trial.org_id is not None:
            if trial.org_id != org_id:
                raise HTTPException(
                    status_code=404, detail=f"Trial {trial_id} not found"
                )
        else:
            # Fallback for legacy rows where trial.org_id is not populated.
            task_org_result = await session.execute(
                select(TaskModel.org_id).where(TaskModel.id == trial.task_id)
            )
            task_org_id = task_org_result.scalar_one_or_none()
            if task_org_id != org_id:
                raise HTTPException(
                    status_code=404, detail=f"Trial {trial_id} not found"
                )

    return trial


def _reset_task_verdict(task: TaskModel) -> None:
    """Clear cached verdict state before re-running analysis or verdict."""
    task.verdict = None
    task.verdict_status = None
    task.verdict_error = None
    task.verdict_started_at = None
    task.verdict_finished_at = None

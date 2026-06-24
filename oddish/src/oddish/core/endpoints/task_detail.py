from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from oddish.core.endpoints._common import get_task_for_org_core
from oddish.core.helpers import (
    build_task_status_response,
    build_trial_response,
    fetch_trial_queue_info,
    fetch_visible_worker_jobs,
)
from oddish.core.tags_projection import (
    list_direct_version_tags,
    list_effective_user_tags_for_task_versions,
)
from oddish.db import TaskModel, TaskVersionModel, TrialStatus
from oddish.schemas import (
    TaskCostTotals,
    TaskDetailResponse,
    TaskVersionResponse,
    TaskVersionSummary,
    UserTagRef,
)


async def list_task_versions_core(
    session: AsyncSession,
    *,
    task_id: str,
    org_id: str | None = None,
    task: TaskModel | None = None,
) -> list[TaskVersionResponse]:
    """Return all versions of a task, newest first."""
    if task is None:
        task = await get_task_for_org_core(session, task_id=task_id, org_id=org_id)

    result = await session.execute(
        select(TaskVersionModel)
        .where(TaskVersionModel.task_id == task.id)
        .order_by(TaskVersionModel.version.desc())
    )
    versions = result.scalars().all()
    return [TaskVersionResponse.model_validate(v) for v in versions]


async def get_task_version_core(
    session: AsyncSession,
    *,
    task_id: str,
    version: int,
    org_id: str | None = None,
) -> TaskVersionResponse:
    """Return a specific version of a task."""
    task = await get_task_for_org_core(session, task_id=task_id, org_id=org_id)

    result = await session.execute(
        select(TaskVersionModel).where(
            TaskVersionModel.task_id == task.id,
            TaskVersionModel.version == version,
        )
    )
    version_row = result.scalar_one_or_none()
    if not version_row:
        raise HTTPException(
            status_code=404,
            detail=f"Version {version} not found for task {task_id}",
        )
    return TaskVersionResponse.model_validate(version_row)


async def get_task_detail_core(
    session: AsyncSession,
    *,
    task_id: str,
    org_id: str | None = None,
) -> TaskDetailResponse:
    query = (
        select(TaskModel)
        .options(
            selectinload(TaskModel.experiments),
            selectinload(TaskModel.trials),
        )
        .where(TaskModel.id == task_id)
    )
    if org_id is not None:
        query = query.where(TaskModel.org_id == org_id)
    task = (await session.execute(query)).scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    queue_info_by_trial_id = await fetch_trial_queue_info(session, trials=task.trials)
    jobs_by_subject = await fetch_visible_worker_jobs(
        session,
        task_ids=[task.id],
        trial_ids=[trial.id for trial in task.trials],
    )

    # Header counts stay current-version-scoped (matches /tasks list);
    # trials below are widened to span every version so the frontend
    # switcher and per-version rollups see them all.
    task_status = build_task_status_response(
        task,
        include_empty_rewards=True,
        queue_info_by_trial_id=queue_info_by_trial_id,
        jobs_by_subject=jobs_by_subject,
    )
    all_trial_models = [t for t in task.trials if t.superseded_by_trial_id is None]
    task_status.trials = [
        build_trial_response(
            t,
            task.task_path,
            queue_info=queue_info_by_trial_id.get(t.id),
            jobs=jobs_by_subject.get(("trials", t.id), []),
        )
        for t in all_trial_models
    ]

    version_rows = await list_task_versions_core(
        session, task_id=task_id, org_id=org_id, task=task
    )

    totals, versions_sorted = _aggregate_task_detail_rollups(
        trials=task_status.trials or [],
        version_rows=version_rows,
        current_version_id=task_status.current_version_id,
    )

    # Hydrate effective user tags so the detail page renders the same
    # chips the browse list does.
    user_tags_by_task = await list_effective_user_tags_for_task_versions(
        session, task_ids=[task.id], public_only=False
    )
    task_status.user_tags = [
        UserTagRef(
            tag_id=t.tag_id,
            key=t.key,
            value=t.value,
            color=t.color,
            visibility=t.visibility,
            current=t.current,
            older=t.older,
        )
        for t in user_tags_by_task.get(task.id, [])
    ]

    # Per-version direct tags, so the version switcher's tag editor shows
    # the selected version's own chips (distinct from the task-level union).
    version_tags = await list_direct_version_tags(
        session, version_ids=[v.id for v in versions_sorted]
    )
    for summary in versions_sorted:
        summary.user_tags = [
            UserTagRef(
                tag_id=t.tag_id,
                key=t.key,
                value=t.value,
                color=t.color,
                visibility=t.visibility,
                current=t.current,
                older=t.older,
            )
            for t in version_tags.get(summary.id, [])
        ]

    return TaskDetailResponse(
        task=task_status,
        versions=versions_sorted,
        totals=totals,
    )


def _aggregate_task_detail_rollups(
    *,
    trials,
    version_rows,
    current_version_id: str | None,
) -> tuple[TaskCostTotals, list[TaskVersionSummary]]:
    """Fold trials into a task-wide cost rollup + per-version summaries.

    Pulled out so it's unit-testable without standing up the full
    ``get_task_detail_core`` query stack.
    """
    summary_by_version_id: dict[str, TaskVersionSummary] = {
        v.id: TaskVersionSummary(
            id=v.id,
            version=v.version,
            message=v.message,
            created_at=v.created_at,
            is_current=(v.id == current_version_id),
        )
        for v in version_rows
    }

    totals = TaskCostTotals()
    for trial in trials:
        totals.total_trials += 1
        if trial.cost_usd is not None:
            totals.cost_usd += trial.cost_usd
            totals.cost_trial_count += 1
            if trial.cost_is_estimated:
                totals.cost_has_estimated = True
            else:
                totals.cost_has_native = True

        bucket = summary_by_version_id.get(trial.task_version_id or "")
        if bucket is None:
            continue
        bucket.trial_count += 1
        if trial.status == TrialStatus.SUCCESS:
            bucket.completed_count += 1
        elif trial.status == TrialStatus.FAILED:
            bucket.failed_count += 1

        if trial.status == TrialStatus.SUCCESS and trial.reward is not None:
            bucket.reward_sum += trial.reward
            bucket.reward_total += 1
            if trial.reward == 1:
                bucket.pass_count += 1
            elif trial.reward == 0:
                bucket.fail_count += 1
            else:
                bucket.partial_count += 1
        elif trial.status != TrialStatus.FAILED:
            bucket.pending_count += 1

        if trial.cost_usd is not None:
            bucket.cost_usd += trial.cost_usd
            bucket.cost_trial_count += 1
            if trial.cost_is_estimated:
                bucket.cost_has_estimated = True
            else:
                bucket.cost_has_native = True

        candidate = trial.finished_at or trial.started_at or trial.created_at
        if candidate is not None and (
            bucket.last_run_at is None or candidate > bucket.last_run_at
        ):
            bucket.last_run_at = candidate

    versions_sorted = sorted(
        summary_by_version_id.values(),
        key=lambda s: s.version,
        reverse=True,
    )
    return totals, versions_sorted

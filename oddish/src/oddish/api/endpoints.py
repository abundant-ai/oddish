from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import and_, case, delete, func, nulls_last, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.orm import load_only, selectinload

from oddish.api.helpers import (
    build_task_status_response_compact,
    build_task_status_response,
    build_task_status_responses_from_counts,
    build_trial_response,
    fetch_trial_queue_info,
    fetch_trial_analysis_summaries,
)
from collections.abc import Collection
from harbor.models.environment_type import EnvironmentType
from oddish.api.trial_io import (
    read_trial_logs,
    read_trial_logs_structured,
    read_trial_result,
    read_trial_trajectory,
)
from oddish.db import (
    AnalysisStatus,
    ExperimentModel,
    TaskModel,
    TaskStatus,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    VerdictStatus,
)
from oddish.schemas import (
    TaskBrowseExperiment,
    TaskBrowseItem,
    TaskBrowseResponse,
    TaskBrowseTrial,
    TaskStatusResponse,
    TaskSweepSubmission,
    TaskVersionResponse,
    TrialResponse,
)


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
    task: TaskModel | None = result.scalar_one_or_none()
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
                TrialModel.task_version_id,
                TrialModel.experiment_id,
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
                TrialModel.phase_timing,
                TrialModel.analysis_status,
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
                    TaskModel.current_version_id,
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
        has_trials_in_experiment = (
            select(TrialModel.task_id)
            .where(TrialModel.experiment_id == experiment_id)
            .distinct()
            .correlate(None)
            .scalar_subquery()
        )
        query = query.where(
            or_(
                TaskModel.experiment_id == experiment_id,
                TaskModel.id.in_(has_trials_in_experiment),
            )
        )

    query = query.limit(limit).offset(offset)
    result = await session.execute(query)
    tasks = result.scalars().all()

    # When scoped to an experiment, only include trials that belong to it.
    if experiment_id and include_trials:
        from sqlalchemy.orm.attributes import set_committed_value

        for task in tasks:
            filtered = [
                t
                for t in task.trials
                if t.experiment_id == experiment_id or t.experiment_id is None
            ]
            set_committed_value(task, "trials", filtered)

    if include_trials:
        queue_info_by_trial_id = await fetch_trial_queue_info(
            session,
            trials=[trial for task in tasks for trial in task.trials],
        )
        if compact_trials:
            analysis_summaries = await fetch_trial_analysis_summaries(
                session, task_ids=[task.id for task in tasks]
            )
            return [
                build_task_status_response_compact(
                    task,
                    include_empty_rewards=include_empty_rewards,
                    analysis_summaries=analysis_summaries,
                    queue_info_by_trial_id=queue_info_by_trial_id,
                )
                for task in tasks
            ]
        return [
            build_task_status_response(
                task,
                include_empty_rewards=include_empty_rewards,
                queue_info_by_trial_id=queue_info_by_trial_id,
            )
            for task in tasks
        ]

    return await build_task_status_responses_from_counts(
        session,
        tasks=tasks,
        include_empty_rewards=include_empty_rewards,
    )


async def browse_tasks_core(
    session: AsyncSession,
    *,
    org_id: str | None = None,
    limit: int = 25,
    offset: int = 0,
    query: str | None = None,
) -> TaskBrowseResponse:
    """List latest-version task summaries for the task browser."""

    current_version = aliased(TaskVersionModel)
    normalized_query = query.strip() if query else None

    ranked_tasks = (
        select(
            TaskModel.id.label("task_id"),
            TaskModel.name.label("name"),
            TaskModel.current_version_id.label("current_version_id"),
            current_version.version.label("current_version"),
            TaskModel.created_at.label("created_at"),
            func.row_number()
            .over(
                partition_by=TaskModel.name,
                order_by=(
                    nulls_last(current_version.version.desc()),
                    TaskModel.created_at.desc(),
                    TaskModel.id.desc(),
                ),
            )
            .label("name_rank"),
        )
        .select_from(TaskModel)
        .outerjoin(current_version, current_version.id == TaskModel.current_version_id)
    )
    if org_id is not None:
        ranked_tasks = ranked_tasks.where(TaskModel.org_id == org_id)
    if normalized_query:
        ranked_tasks = ranked_tasks.where(TaskModel.name.ilike(f"%{normalized_query}%"))
    ranked_tasks_subquery = ranked_tasks.subquery()

    version_counts = (
        select(
            TaskVersionModel.task_id.label("task_id"),
            func.count(TaskVersionModel.id).label("version_count"),
        )
        .group_by(TaskVersionModel.task_id)
        .subquery()
    )

    trial_activity_at = func.greatest(
        func.coalesce(TrialModel.finished_at, TrialModel.created_at),
        func.coalesce(TrialModel.started_at, TrialModel.created_at),
        TrialModel.created_at,
    )
    trial_agg_query = select(
        TrialModel.task_id.label("task_id"),
        TrialModel.task_version_id.label("task_version_id"),
        func.count(TrialModel.id).label("total_trials"),
        func.count(case((TrialModel.status == TrialStatus.SUCCESS, 1))).label(
            "completed_trials"
        ),
        func.count(case((TrialModel.status == TrialStatus.FAILED, 1))).label(
            "failed_trials"
        ),
        func.count(case((TrialModel.reward == 1, 1))).label("reward_success"),
        func.count(case((TrialModel.reward.isnot(None), 1))).label("reward_total"),
        func.max(trial_activity_at).label("last_run_at"),
    )
    if org_id is not None:
        trial_agg_query = trial_agg_query.where(TrialModel.org_id == org_id)
    trial_aggregates = trial_agg_query.group_by(
        TrialModel.task_id, TrialModel.task_version_id
    ).subquery()

    paged_rows = (
        select(
            ranked_tasks_subquery.c.task_id,
            ranked_tasks_subquery.c.name,
            ranked_tasks_subquery.c.current_version,
            ranked_tasks_subquery.c.current_version_id,
            func.coalesce(version_counts.c.version_count, 0).label("version_count"),
            func.coalesce(trial_aggregates.c.total_trials, 0).label("total_trials"),
            func.coalesce(trial_aggregates.c.completed_trials, 0).label(
                "completed_trials"
            ),
            func.coalesce(trial_aggregates.c.failed_trials, 0).label("failed_trials"),
            func.coalesce(trial_aggregates.c.reward_success, 0).label("reward_success"),
            func.coalesce(trial_aggregates.c.reward_total, 0).label("reward_total"),
            trial_aggregates.c.last_run_at.label("last_run_at"),
        )
        .select_from(ranked_tasks_subquery)
        .outerjoin(
            version_counts, version_counts.c.task_id == ranked_tasks_subquery.c.task_id
        )
        .outerjoin(
            trial_aggregates,
            and_(
                trial_aggregates.c.task_id == ranked_tasks_subquery.c.task_id,
                trial_aggregates.c.task_version_id
                == ranked_tasks_subquery.c.current_version_id,
            ),
        )
        .where(ranked_tasks_subquery.c.name_rank == 1)
        .order_by(
            nulls_last(trial_aggregates.c.last_run_at.desc()),
            nulls_last(ranked_tasks_subquery.c.current_version.desc()),
            ranked_tasks_subquery.c.name.asc(),
        )
        .limit(limit + 1)
        .offset(offset)
    )

    result = await session.execute(paged_rows)
    raw_rows = result.mappings().all()
    has_more = len(raw_rows) > limit
    visible_rows = raw_rows[:limit]

    experiments_by_task: dict[str, list[TaskBrowseExperiment]] = {}
    latest_trials_by_task: dict[str, list[TaskBrowseTrial]] = {}
    task_version_pairs = [
        (str(row["task_id"]), str(row["current_version_id"]))
        for row in visible_rows
        if row["current_version_id"] is not None
    ]

    if task_version_pairs:
        exp_join_condition = [ExperimentModel.id == TrialModel.experiment_id]
        if org_id is not None:
            exp_join_condition.append(ExperimentModel.org_id == org_id)
        exp_query = (
            select(
                TrialModel.task_id.label("task_id"),
                ExperimentModel.id.label("experiment_id"),
                ExperimentModel.name.label("experiment_name"),
            )
            .select_from(TrialModel)
            .join(ExperimentModel, and_(*exp_join_condition))
            .where(
                TrialModel.experiment_id.isnot(None),
                tuple_(TrialModel.task_id, TrialModel.task_version_id).in_(
                    task_version_pairs
                ),
            )
            .distinct()
            .order_by(
                TrialModel.task_id.asc(),
                ExperimentModel.name.asc(),
                ExperimentModel.id.asc(),
            )
        )
        if org_id is not None:
            exp_query = exp_query.where(TrialModel.org_id == org_id)
        experiment_rows = await session.execute(exp_query)
        for experiment_row in experiment_rows.mappings():
            experiments_by_task.setdefault(str(experiment_row["task_id"]), []).append(
                TaskBrowseExperiment(
                    id=str(experiment_row["experiment_id"]),
                    name=str(experiment_row["experiment_name"]),
                )
            )

        trial_query = (
            select(
                TrialModel.task_id.label("task_id"),
                TrialModel.id.label("trial_id"),
                TrialModel.name.label("trial_name"),
                TrialModel.status.label("trial_status"),
                TrialModel.reward.label("reward"),
                TrialModel.error_message.label("error_message"),
            )
            .where(
                tuple_(TrialModel.task_id, TrialModel.task_version_id).in_(
                    task_version_pairs
                ),
            )
            .order_by(
                TrialModel.task_id.asc(),
                TrialModel.created_at.asc(),
                TrialModel.id.asc(),
            )
        )
        if org_id is not None:
            trial_query = trial_query.where(TrialModel.org_id == org_id)
        latest_trial_rows = await session.execute(trial_query)
        for trial_row in latest_trial_rows.mappings():
            latest_trials_by_task.setdefault(str(trial_row["task_id"]), []).append(
                TaskBrowseTrial(
                    id=str(trial_row["trial_id"]),
                    name=str(trial_row["trial_name"]),
                    status=trial_row["trial_status"],
                    reward=trial_row["reward"],
                    error_message=trial_row["error_message"],
                )
            )

    return TaskBrowseResponse(
        items=[
            TaskBrowseItem(
                id=str(row["task_id"]),
                name=str(row["name"]),
                current_version=(
                    int(row["current_version"])
                    if row["current_version"] is not None
                    else None
                ),
                current_version_id=(
                    str(row["current_version_id"])
                    if row["current_version_id"] is not None
                    else None
                ),
                version_count=int(row["version_count"] or 0),
                total_trials=int(row["total_trials"] or 0),
                completed_trials=int(row["completed_trials"] or 0),
                failed_trials=int(row["failed_trials"] or 0),
                reward_success=int(row["reward_success"] or 0),
                reward_total=int(row["reward_total"] or 0),
                last_run_at=row["last_run_at"],
                latest_trials=latest_trials_by_task.get(str(row["task_id"]), []),
                experiments=experiments_by_task.get(str(row["task_id"]), []),
            )
            for row in visible_rows
        ],
        limit=limit,
        offset=offset,
        has_more=has_more,
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
        queue_info_by_trial_id = await fetch_trial_queue_info(
            session, trials=task.trials
        )
        return build_task_status_response(
            task,
            include_empty_rewards=include_empty_rewards,
            queue_info_by_trial_id=queue_info_by_trial_id,
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

    queue_info_by_trial_id = await fetch_trial_queue_info(session, trials=[trial])
    return build_trial_response(
        trial,
        task_path,
        queue_info=queue_info_by_trial_id.get(trial.id),
    )


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
    trial.idempotency_key = None
    trial.current_worker_id = None
    trial.current_queue_slot = None
    trial.modal_function_call_id = None

    # Move completed tasks back to running once a trial is requeued.
    if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
        task.status = TaskStatus.RUNNING
        task.finished_at = None

    await session.commit()
    return {"status": "queued", "trial_id": trial_id}


def _reset_task_verdict(task: TaskModel) -> None:
    """Clear cached verdict state before re-running analysis or verdict."""
    task.verdict = None
    task.verdict_status = None
    task.verdict_error = None
    task.verdict_started_at = None
    task.verdict_finished_at = None
    task.verdict_modal_function_call_id = None


def _reset_trial_analysis(trial: TrialModel) -> None:
    """Clear cached analysis state before re-running analysis."""
    trial.analysis = None
    trial.analysis_status = None
    trial.analysis_error = None
    trial.analysis_started_at = None
    trial.analysis_finished_at = None
    trial.analysis_modal_function_call_id = None


async def _count_active_trials(session: AsyncSession, *, task_id: str) -> int:
    """Count non-terminal trials for a task."""
    active_statuses = [
        TrialStatus.PENDING,
        TrialStatus.QUEUED,
        TrialStatus.RUNNING,
        TrialStatus.RETRYING,
    ]
    count = await session.scalar(
        select(func.count(TrialModel.id)).where(
            TrialModel.task_id == task_id,
            TrialModel.status.in_(active_statuses),
        )
    )
    return int(count or 0)


async def rerun_trial_analysis_core(
    session: AsyncSession,
    *,
    trial_id: str,
    org_id: str | None = None,
) -> dict[str, str]:
    """Queue analysis for a completed trial and invalidate the task verdict."""
    trial = await get_trial_for_org_core(session, trial_id=trial_id, org_id=org_id)
    task = await session.get(TaskModel, trial.task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")

    if trial.status not in (TrialStatus.SUCCESS, TrialStatus.FAILED):
        raise HTTPException(
            status_code=400,
            detail=(
                "Can only run analysis for completed or failed trials "
                f"(current: {trial.status.value})"
            ),
        )

    if trial.analysis_status in (
        AnalysisStatus.PENDING,
        AnalysisStatus.QUEUED,
        AnalysisStatus.RUNNING,
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Analysis is already in progress for this trial "
                f"(current: {trial.analysis_status.value})"
            ),
        )

    active_trials = await _count_active_trials(session, task_id=task.id)
    if active_trials > 0:
        raise HTTPException(
            status_code=400,
            detail="Can only run trial analysis after all trials for the task finish",
        )

    if task.verdict_status in (
        VerdictStatus.PENDING,
        VerdictStatus.QUEUED,
        VerdictStatus.RUNNING,
    ):
        raise HTTPException(
            status_code=400,
            detail="Cannot rerun analysis while the task verdict is still running",
        )

    _reset_trial_analysis(trial)
    _reset_task_verdict(task)
    task.run_analysis = True
    task.status = TaskStatus.ANALYZING
    task.finished_at = None
    trial.analysis_status = AnalysisStatus.QUEUED

    await session.commit()
    return {"status": "queued", "trial_id": trial_id}


async def rerun_task_analysis_core(
    session: AsyncSession,
    *,
    task_id: str,
    org_id: str | None = None,
) -> dict[str, str | int]:
    """Queue analysis jobs for every trial in a finished task."""
    result = await session.execute(
        select(TaskModel)
        .options(selectinload(TaskModel.trials))
        .where(TaskModel.id == task_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    if org_id is not None and task.org_id != org_id:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    if not task.trials:
        raise HTTPException(status_code=400, detail="Task has no trials to analyze")

    active_trials = await _count_active_trials(session, task_id=task.id)
    if active_trials > 0:
        raise HTTPException(
            status_code=400,
            detail="Can only run task analysis after all trials finish",
        )

    if any(
        trial.analysis_status
        in (AnalysisStatus.PENDING, AnalysisStatus.QUEUED, AnalysisStatus.RUNNING)
        for trial in task.trials
    ):
        raise HTTPException(
            status_code=400,
            detail="Some trial analyses are already in progress for this task",
        )

    if task.verdict_status in (
        VerdictStatus.PENDING,
        VerdictStatus.QUEUED,
        VerdictStatus.RUNNING,
    ):
        raise HTTPException(
            status_code=400,
            detail="Cannot rerun analysis while the task verdict is still running",
        )

    for trial in task.trials:
        _reset_trial_analysis(trial)
        trial.analysis_status = AnalysisStatus.QUEUED

    _reset_task_verdict(task)
    task.run_analysis = True
    task.status = TaskStatus.ANALYZING
    task.finished_at = None

    await session.commit()
    return {
        "status": "queued",
        "task_id": task_id,
        "trial_count": len(task.trials),
    }


async def rerun_task_verdict_core(
    session: AsyncSession,
    *,
    task_id: str,
    org_id: str | None = None,
) -> dict[str, str]:
    """Queue a fresh verdict job for a finished task."""
    result = await session.execute(
        select(TaskModel)
        .options(selectinload(TaskModel.trials))
        .where(TaskModel.id == task_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    if org_id is not None and task.org_id != org_id:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    if not task.trials:
        raise HTTPException(status_code=400, detail="Task has no trials")

    active_trials = await _count_active_trials(session, task_id=task.id)
    if active_trials > 0:
        raise HTTPException(
            status_code=400,
            detail="Can only run a task verdict after all trials finish",
        )

    if any(
        trial.analysis_status
        in (None, AnalysisStatus.PENDING, AnalysisStatus.QUEUED, AnalysisStatus.RUNNING)
        for trial in task.trials
    ):
        raise HTTPException(
            status_code=400,
            detail="All trial analyses must finish before running a task verdict",
        )

    if task.verdict_status in (
        VerdictStatus.PENDING,
        VerdictStatus.QUEUED,
        VerdictStatus.RUNNING,
    ):
        raise HTTPException(
            status_code=400,
            detail="Task verdict is already in progress",
        )

    _reset_task_verdict(task)
    task.run_analysis = True
    task.status = TaskStatus.VERDICT_PENDING
    task.finished_at = None
    task.verdict_status = VerdictStatus.QUEUED
    task.verdict_started_at = None
    task.verdict_finished_at = None

    await session.commit()
    return {"status": "queued", "task_id": task_id}


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


# =============================================================================
# Task Version Helpers
# =============================================================================


async def list_task_versions_core(
    session: AsyncSession,
    *,
    task_id: str,
    org_id: str | None = None,
) -> list[TaskVersionResponse]:
    """Return all versions of a task, newest first."""
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


async def delete_task_core(
    session: AsyncSession,
    *,
    task_id: str,
    org_id: str | None = None,
) -> dict:
    """Delete a task and its trials with optional org scoping."""
    task = await get_task_for_org_core(session, task_id=task_id, org_id=org_id)
    
    trial_rows_result = await session.execute(
        select(TrialModel.id, TrialModel.trial_s3_key).where(
            TrialModel.task_id == task.id
        )
    )
    trial_rows = [(row[0], row[1]) for row in trial_rows_result.all()]
    
    from oddish.db.storage import collect_s3_prefixes_for_deletion
    s3_prefixes = collect_s3_prefixes_for_deletion(
        tasks=[(task.task_s3_key, task.task_path)],
        trials=trial_rows,
    )

    await session.delete(task)
    
    return {
        "s3_prefixes": s3_prefixes,
        "deleted": {"task_id": task_id}
    }


async def delete_experiment_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    org_id: str | None = None,
) -> dict:
    """Delete an experiment and all associated tasks/trials with optional org scoping."""
    query = select(ExperimentModel).where(ExperimentModel.id == experiment_id)
    if org_id is not None:
        query = query.where(ExperimentModel.org_id == org_id)
    
    result = await session.execute(query)
    experiment = result.scalar_one_or_none()
    if not experiment:
        raise HTTPException(
            status_code=404, detail=f"Experiment {experiment_id} not found"
        )

    task_ids_query = select(TaskModel.id).where(TaskModel.experiment_id == experiment_id)
    if org_id is not None:
        task_ids_query = task_ids_query.where(TaskModel.org_id == org_id)
    
    task_ids_result = await session.execute(task_ids_query)
    task_ids = [row[0] for row in task_ids_result.all()]

    task_rows_query = select(TaskModel.task_s3_key, TaskModel.task_path).where(
        TaskModel.experiment_id == experiment_id
    )
    if org_id is not None:
        task_rows_query = task_rows_query.where(TaskModel.org_id == org_id)
    
    task_rows_result = await session.execute(task_rows_query)
    task_rows = [(row[0], row[1]) for row in task_rows_result.all()]

    trial_rows: list[tuple[str, str | None]] = []
    if task_ids:
        trial_rows_result = await session.execute(
            select(TrialModel.id, TrialModel.trial_s3_key).where(
                TrialModel.task_id.in_(task_ids)
            )
        )
        trial_rows = [(row[0], row[1]) for row in trial_rows_result.all()]

    trial_only_query = select(TrialModel.id, TrialModel.trial_s3_key).where(
        TrialModel.experiment_id == experiment_id
    )
    if org_id is not None:
        trial_only_query = trial_only_query.where(TrialModel.org_id == org_id)
    if task_ids:
        trial_only_query = trial_only_query.where(~TrialModel.task_id.in_(task_ids))

    trial_only_result = await session.execute(trial_only_query)
    extra_trial_rows = [(r[0], r[1]) for r in trial_only_result.all()]
    trial_rows.extend(extra_trial_rows)

    from oddish.db.storage import collect_s3_prefixes_for_deletion
    s3_prefixes = collect_s3_prefixes_for_deletion(
        tasks=task_rows,
        trials=trial_rows,
    )

    deleted_trials = 0
    if task_ids:
        trials_result = await session.execute(
            delete(TrialModel).where(TrialModel.task_id.in_(task_ids))
        )
        deleted_trials += int(trials_result.rowcount or 0)

    trial_only_del_query = delete(TrialModel).where(TrialModel.experiment_id == experiment_id)
    if org_id is not None:
        trial_only_del_query = trial_only_del_query.where(TrialModel.org_id == org_id)
    trial_only_del_result = await session.execute(trial_only_del_query)
    deleted_trials += int(trial_only_del_result.rowcount or 0)

    tasks_del_query = delete(TaskModel).where(TaskModel.experiment_id == experiment_id)
    if org_id is not None:
        tasks_del_query = tasks_del_query.where(TaskModel.org_id == org_id)
    tasks_result = await session.execute(tasks_del_query)
    deleted_tasks = int(tasks_result.rowcount or 0)

    experiments_del_query = delete(ExperimentModel).where(ExperimentModel.id == experiment_id)
    if org_id is not None:
        experiments_del_query = experiments_del_query.where(ExperimentModel.org_id == org_id)
    experiments_result = await session.execute(experiments_del_query)
    deleted_experiments = int(experiments_result.rowcount or 0)

    return {
        "s3_prefixes": s3_prefixes,
        "deleted": {
            "trials": deleted_trials,
            "tasks": deleted_tasks,
            "experiments": deleted_experiments,
        }
    }


async def create_task_sweep_core(
    session: AsyncSession,
    *,
    submission: TaskSweepSubmission,
    org_id: str | None = None,
    default_environment: EnvironmentType | None = None,
    allowed_environments: Collection[EnvironmentType] | None = None,
) -> tuple[TaskModel, list[TrialModel], bool, ExperimentModel | None]:
    """
    Expands a sweep submission into trials and either appends to an existing task
    or creates a new one.

    Returns a tuple of (task, new_trials, is_append, experiment).
    """
    from oddish.api.sweeps import (
        build_trial_specs_from_sweep,
        build_task_submission_from_sweep,
    )
    from oddish.queue import (
        append_trials_to_task,
        create_task,
        get_experiment_by_id_or_name,
        get_or_create_experiment,
    )
    from oddish.api.tasks import resolve_task_storage
    from oddish.task_timeouts import TaskTimeoutValidationError

    # Auto-detect append mode if the task already exists in the DB for this org.
    if not submission.append_to_task:
        existing = await session.get(TaskModel, submission.task_id)
        if existing is not None and (org_id is None or existing.org_id == org_id):
            submission = submission.model_copy(update={"append_to_task": True})

    if submission.append_to_task:
        task = await get_task_for_org_core(session, task_id=submission.task_id, org_id=org_id)
        if task.status in (TaskStatus.ANALYZING, TaskStatus.VERDICT_PENDING):
            raise HTTPException(
                status_code=400,
                detail="Cannot append trials while task analysis or verdict is in progress",
            )
        if submission.run_analysis and not task.run_analysis:
            raise HTTPException(
                status_code=400,
                detail="Cannot enable run_analysis when appending to a task that was created without it",
            )

        new_experiment_id: str | None = None
        experiment: ExperimentModel | None = None
        if submission.experiment_id:
            experiment = await get_experiment_by_id_or_name(session, submission.experiment_id, org_id)
            if not experiment:
                experiment = await get_or_create_experiment(session, submission.experiment_id, org_id)
            new_experiment_id = experiment.id
        else:
            experiment = await get_experiment_by_id_or_name(session, task.experiment_id, org_id)

        # Determine default environment from existing trial, if present.
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
        effective_default_env = (
            EnvironmentType(existing_environment) if existing_environment else default_environment
        )

        trials = build_trial_specs_from_sweep(
            submission,
            default_environment=effective_default_env,
            allowed_environments=allowed_environments,
        )
        
        append_submission = submission.model_copy(
            update={
                "name": task.name,
                "priority": task.priority,
                "experiment_id": new_experiment_id or task.experiment_id,
                "tags": task.tags or {},
                "run_analysis": task.run_analysis,
                "user": task.user,
            }
        )
        expanded = build_task_submission_from_sweep(
            append_submission, task_path=task.task_path, trials=trials
        )
        new_trials = await append_trials_to_task(
            session,
            task=task,
            submission=expanded,
            experiment_id=new_experiment_id,
        )
        
        return task, new_trials, True, experiment

    # Create mode
    task_path, task_s3_key = await resolve_task_storage(
        submission.task_id,
        s3_missing_detail=(
            f"Task {submission.task_id} not found in S3. "
            "Upload it first with POST /tasks/upload"
        ),
        local_missing_detail=(
            f"Task {submission.task_id} not found in local storage. "
            "Upload it first with POST /tasks/upload"
        ),
    )
    trials = build_trial_specs_from_sweep(
        submission,
        default_environment=default_environment,
        allowed_environments=allowed_environments,
    )
    expanded = build_task_submission_from_sweep(
        submission, task_path=task_path, trials=trials
    )
    
    try:
        task = await create_task(session, expanded, task_id=submission.task_id, org_id=org_id)
    except TaskTimeoutValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if task_s3_key:
        task.task_s3_key = task_s3_key
        
    from oddish.queue import get_experiment_by_id_or_name
    experiment = (
        await get_experiment_by_id_or_name(session, task.experiment_id, org_id)
        if task.experiment_id else None
    )

    return task, list(task.trials), False, experiment

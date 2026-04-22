from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import and_, case, delete, func, nulls_last, or_, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.orm import load_only, selectinload

from oddish.core.helpers import (
    build_task_status_response_compact,
    build_task_status_response,
    build_task_status_responses_from_counts,
    build_trial_response,
    fetch_experiment_effective_version_ids,
    fetch_trial_queue_info,
    fetch_trial_analysis_summaries,
    get_task_status_trials,
    resolve_effective_version_id,
)
from collections.abc import Collection
from harbor.models.environment_type import EnvironmentType
from oddish.core.trial_io import (
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
from oddish.timing import TimingRecorder, elapsed_ms, now


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
    record_timing: TimingRecorder | None = None,
) -> list[TaskStatusResponse]:
    """List tasks with optional filters and aggregated trial stats."""
    query = select(TaskModel).order_by(TaskModel.created_at.desc())
    if include_trials:
        trials_loader = selectinload(TaskModel.trials)
        experiments_loader = selectinload(TaskModel.experiments)
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
                # ``origin`` is surfaced in compact responses, so it must
                # be loaded eagerly; otherwise the response builder
                # triggers a lazy-load attempt outside the async
                # greenlet and fails with MissingGreenlet.
                TrialModel.origin,
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
            experiments_loader = experiments_loader.load_only(
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
                    TaskModel.run_analysis,
                    TaskModel.verdict_status,
                    TaskModel.verdict,
                    TaskModel.verdict_error,
                    TaskModel.created_at,
                    TaskModel.started_at,
                    TaskModel.finished_at,
                ),
                trials_loader,
                experiments_loader,
            )
        else:
            query = query.options(trials_loader, experiments_loader)
    else:
        query = query.options(selectinload(TaskModel.experiments))

    if org_id is not None:
        query = query.where(TaskModel.org_id == org_id)
    if status:
        query = query.where(TaskModel.status == status)
    if user:
        query = query.where(TaskModel.user == user)
    if experiment_id:
        query = query.where(
            TaskModel.experiments.any(ExperimentModel.id == experiment_id)
        )

    query = query.limit(limit).offset(offset)
    query_started_at = now()
    result = await session.execute(query)
    if record_timing is not None:
        record_timing(
            "tasks_query",
            elapsed_ms(query_started_at),
            "List tasks query",
        )
    tasks = result.scalars().all()

    # When trial payloads are loaded, constrain them to the subset the status UI
    # should reflect: first the requested experiment, then the task's active
    # version within that experiment.  Within an experiment the "active version"
    # is the latest version that has trials in that experiment — not the task's
    # global ``current_version_id`` — so an experiment still shows its own
    # trials after the underlying task is re-uploaded elsewhere.
    if include_trials:
        from sqlalchemy.orm.attributes import set_committed_value

        for task in tasks:
            if experiment_id:
                scoped_trials = [
                    t for t in task.trials if t.experiment_id == experiment_id
                ]
                set_committed_value(task, "trials", scoped_trials)
                effective = resolve_effective_version_id(
                    task, experiment_context_id=experiment_id
                )
                set_committed_value(
                    task,
                    "trials",
                    get_task_status_trials(task, version_id=effective),
                )
            else:
                set_committed_value(task, "trials", get_task_status_trials(task))

    if include_trials:
        queue_info_started_at = now()
        queue_info_by_trial_id = await fetch_trial_queue_info(
            session,
            trials=[trial for task in tasks for trial in task.trials],
        )
        if record_timing is not None:
            record_timing(
                "tasks_queue_info",
                elapsed_ms(queue_info_started_at),
                "Trial queue info",
            )
        if compact_trials:
            analysis_started_at = now()
            analysis_summaries = await fetch_trial_analysis_summaries(
                session, task_ids=[task.id for task in tasks]
            )
            if record_timing is not None:
                record_timing(
                    "tasks_analysis",
                    elapsed_ms(analysis_started_at),
                    "Trial analysis summaries",
                )
            build_started_at = now()
            response = [
                build_task_status_response_compact(
                    task,
                    include_empty_rewards=include_empty_rewards,
                    analysis_summaries=analysis_summaries,
                    queue_info_by_trial_id=queue_info_by_trial_id,
                    experiment_context_id=experiment_id,
                )
                for task in tasks
            ]
            if record_timing is not None:
                record_timing(
                    "tasks_build",
                    elapsed_ms(build_started_at),
                    "Build compact task response",
                )
            return response
        build_started_at = now()
        response = [
            build_task_status_response(
                task,
                include_empty_rewards=include_empty_rewards,
                queue_info_by_trial_id=queue_info_by_trial_id,
                experiment_context_id=experiment_id,
            )
            for task in tasks
        ]
        if record_timing is not None:
            record_timing(
                "tasks_build",
                elapsed_ms(build_started_at),
                "Build task response",
            )
        return response

    build_started_at = now()
    effective_version_id_by_task_id: dict[str, str] = {}
    if experiment_id and tasks:
        effective_version_id_by_task_id = (
            await fetch_experiment_effective_version_ids(
                session,
                experiment_id=experiment_id,
                task_ids=[task.id for task in tasks],
            )
        )
    response = await build_task_status_responses_from_counts(
        session,
        tasks=tasks,
        include_empty_rewards=include_empty_rewards,
        experiment_context_id=experiment_id,
        effective_version_id_by_task_id=effective_version_id_by_task_id or None,
    )
    if record_timing is not None:
        record_timing(
            "tasks_build",
            elapsed_ms(build_started_at),
            "Build task counts response",
        )
    return response


async def browse_tasks_core(
    session: AsyncSession,
    *,
    org_id: str | None = None,
    limit: int = 25,
    offset: int = 0,
    query: str | None = None,
    record_timing: TimingRecorder | None = None,
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
        func.sum(TrialModel.reward).label("reward_sum"),
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
            func.coalesce(trial_aggregates.c.reward_sum, 0.0).label("reward_sum"),
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
            # Fresh "never run" tasks should appear near the top of the
            # browser (ordered by upload time), not buried below every
            # real experiment. Fall back to the task's created_at when
            # no trials have finished yet.
            func.coalesce(
                trial_aggregates.c.last_run_at,
                ranked_tasks_subquery.c.created_at,
            ).desc(),
            nulls_last(ranked_tasks_subquery.c.current_version.desc()),
            ranked_tasks_subquery.c.name.asc(),
        )
        .limit(limit + 1)
        .offset(offset)
    )

    page_started_at = now()
    result = await session.execute(paged_rows)
    if record_timing is not None:
        record_timing(
            "browse_page",
            elapsed_ms(page_started_at),
            "Browse tasks page query",
        )
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
        experiments_started_at = now()
        experiment_rows = await session.execute(exp_query)
        if record_timing is not None:
            record_timing(
                "browse_experiments",
                elapsed_ms(experiments_started_at),
                "Browse experiment query",
            )
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
        trials_started_at = now()
        latest_trial_rows = await session.execute(trial_query)
        if record_timing is not None:
            record_timing(
                "browse_trials",
                elapsed_ms(trials_started_at),
                "Browse trials query",
            )
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

    build_started_at = now()
    response = TaskBrowseResponse(
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
                reward_sum=float(row["reward_sum"] or 0.0),
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
    if record_timing is not None:
        record_timing(
            "browse_build",
            elapsed_ms(build_started_at),
            "Build browse response",
        )
    return response


async def get_task_status_core(
    session: AsyncSession,
    *,
    task_id: str,
    include_trials: bool = True,
    include_empty_rewards: bool = True,
    org_id: str | None = None,
) -> TaskStatusResponse:
    """Get task status with optional org scoping."""
    query = select(TaskModel).options(selectinload(TaskModel.experiments))
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
        from sqlalchemy.orm.attributes import set_committed_value

        set_committed_value(task, "trials", get_task_status_trials(task))
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

    # Move completed tasks back to running once a trial is requeued.
    if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
        task.status = TaskStatus.RUNNING
        task.finished_at = None

    # Cancel any in-flight TRIAL worker_job for this trial before
    # enqueueing the new one. Without this, a "stuck RUNNING" retry
    # leaves two non-terminal worker_jobs rows with the same
    # ``subject_id`` -- the old stuck row still holds a queue_slot
    # lease until cleanup reaps it, and when it does the handler's
    # outcome can race with the new attempt. Same pattern as
    # ``append_trials_to_task`` uses for superseded VERDICT rows.
    await session.execute(
        text(
            """
            UPDATE worker_jobs
            SET    status = 'CANCELLED',
                   finished_at = NOW(),
                   error_message = 'Superseded by user retry',
                   current_worker_id = NULL,
                   current_queue_slot = NULL,
                   modal_function_call_id = NULL
            WHERE  kind::text = 'TRIAL'
              AND  subject_table = 'trials'
              AND  subject_id = :trial_id
              AND  status::text IN ('QUEUED', 'RETRYING', 'RUNNING', 'BLOCKED')
            """
        ),
        {"trial_id": trial_id},
    )

    # Imported lazily to avoid a circular import through
    # ``oddish.queue`` -> ``oddish.workers.jobs.enqueue``.
    from oddish.queue import enqueue_trial_worker_job

    await enqueue_trial_worker_job(
        session,
        trial_id=trial_id,
        queue_key=trial.queue_key,
        org_id=trial.org_id,
        max_attempts=trial.max_attempts,
    )

    await session.commit()
    return {"status": "queued", "trial_id": trial_id}


def _reset_task_verdict(task: TaskModel) -> None:
    """Clear cached verdict state before re-running analysis or verdict."""
    task.verdict = None
    task.verdict_status = None
    task.verdict_error = None
    task.verdict_started_at = None
    task.verdict_finished_at = None


def _reset_trial_analysis(trial: TrialModel) -> None:
    """Clear cached analysis state before re-running analysis."""
    trial.analysis = None
    trial.analysis_status = None
    trial.analysis_error = None
    trial.analysis_started_at = None
    trial.analysis_finished_at = None


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

    from oddish.queue import enqueue_analysis_worker_job

    await enqueue_analysis_worker_job(
        session, trial_id=trial_id, org_id=trial.org_id
    )

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

    from oddish.queue import enqueue_analysis_worker_job

    for trial in task.trials:
        _reset_trial_analysis(trial)
        trial.analysis_status = AnalysisStatus.QUEUED
        await enqueue_analysis_worker_job(
            session, trial_id=trial.id, org_id=trial.org_id
        )

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

    from oddish.queue import enqueue_verdict_worker_job

    await enqueue_verdict_worker_job(
        session, task_id=task_id, org_id=task.org_id
    )

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
    experiment_id: str | None = None,
) -> dict:
    """Delete a task and its trials, optionally scoped to one experiment.

    When ``experiment_id`` is ``None`` the task and all of its trials are
    deleted unconditionally, along with every ``task_experiments`` row
    (via ``ondelete=CASCADE``).

    When ``experiment_id`` is given, only trials whose ``experiment_id``
    matches are removed and the ``(task_id, experiment_id)`` join row is
    deleted. The task itself is only dropped if no trials and no other
    experiment links remain.
    """
    task_query = select(
        TaskModel.id,
        TaskModel.task_s3_key,
        TaskModel.task_path,
    ).where(TaskModel.id == task_id)
    if org_id is not None:
        task_query = task_query.where(TaskModel.org_id == org_id)
    task_result = await session.execute(task_query)
    task_row = task_result.one_or_none()
    if not task_row:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    resolved_task_id, task_s3_key, task_path = task_row

    from oddish.db.storage import collect_s3_prefixes_for_deletion
    from oddish.db import task_experiments

    # Unscoped: legacy "delete everything" behavior and response shape.
    if experiment_id is None:
        trial_rows_result = await session.execute(
            select(TrialModel.id, TrialModel.trial_s3_key).where(
                TrialModel.task_id == resolved_task_id
            )
        )
        trial_rows = [(row[0], row[1]) for row in trial_rows_result.all()]

        s3_prefixes = collect_s3_prefixes_for_deletion(
            tasks=[(task_s3_key, task_path)],
            trials=trial_rows,
        )

        await session.execute(
            delete(TrialModel)
            .where(TrialModel.task_id == resolved_task_id)
            .execution_options(synchronize_session=False)
        )
        await session.execute(
            delete(TaskModel)
            .where(TaskModel.id == resolved_task_id)
            .execution_options(synchronize_session=False)
        )

        return {
            "s3_prefixes": s3_prefixes,
            "deleted": {"task_id": task_id},
        }

    # Scoped delete: only this experiment's trials + the join row.
    scoped_trial_rows = (
        await session.execute(
            select(TrialModel.id, TrialModel.trial_s3_key).where(
                TrialModel.task_id == resolved_task_id,
                TrialModel.experiment_id == experiment_id,
            )
        )
    ).all()
    scoped_trial_ids = [row[0] for row in scoped_trial_rows]

    # Check that this task really belongs to the given experiment.
    link_exists = await session.scalar(
        select(func.count())
        .select_from(task_experiments)
        .where(
            task_experiments.c.task_id == resolved_task_id,
            task_experiments.c.experiment_id == experiment_id,
        )
    )
    if not link_exists and not scoped_trial_ids:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Task {task_id} has no trials in experiment {experiment_id}"
            ),
        )

    # Cancel live worker_jobs for those trials so workers release slots.
    if scoped_trial_ids:
        await session.execute(
            text(
                """
                UPDATE worker_jobs
                SET    status = 'CANCELLED',
                       finished_at = NOW(),
                       error_message = 'Task deleted by user',
                       current_worker_id = NULL,
                       current_queue_slot = NULL,
                       modal_function_call_id = NULL
                WHERE  subject_table = 'trials'
                  AND  subject_id = ANY(:trial_ids)
                  AND  status::text IN ('QUEUED', 'RETRYING', 'RUNNING', 'BLOCKED')
                """
            ),
            {"trial_ids": scoped_trial_ids},
        )

        await session.execute(
            delete(TrialModel)
            .where(TrialModel.id.in_(scoped_trial_ids))
            .execution_options(synchronize_session=False)
        )

    # Remove the (task_id, experiment_id) association.
    await session.execute(
        delete(task_experiments).where(
            task_experiments.c.task_id == resolved_task_id,
            task_experiments.c.experiment_id == experiment_id,
        )
    )

    # If the task has no remaining trials and no other experiment links, it
    # is now orphaned — drop it outright so the S3 prefix gets cleaned up.
    remaining_trials = int(
        await session.scalar(
            select(func.count(TrialModel.id)).where(
                TrialModel.task_id == resolved_task_id
            )
        )
        or 0
    )
    remaining_links = int(
        await session.scalar(
            select(func.count())
            .select_from(task_experiments)
            .where(task_experiments.c.task_id == resolved_task_id)
        )
        or 0
    )

    task_removed = False
    if remaining_trials == 0 and remaining_links == 0:
        await session.execute(
            text(
                """
                UPDATE worker_jobs
                SET    status = 'CANCELLED',
                       finished_at = NOW(),
                       error_message = 'Task deleted by user',
                       current_worker_id = NULL,
                       current_queue_slot = NULL,
                       modal_function_call_id = NULL
                WHERE  subject_table = 'tasks'
                  AND  subject_id = :task_id
                  AND  status::text IN ('QUEUED', 'RETRYING', 'RUNNING', 'BLOCKED')
                """
            ),
            {"task_id": resolved_task_id},
        )
        await session.execute(
            delete(TaskModel)
            .where(TaskModel.id == resolved_task_id)
            .execution_options(synchronize_session=False)
        )
        s3_prefixes = collect_s3_prefixes_for_deletion(
            tasks=[(task_s3_key, task_path)],
            trials=scoped_trial_rows,
        )
        task_removed = True
    else:
        s3_prefixes = collect_s3_prefixes_for_deletion(
            tasks=[], trials=scoped_trial_rows
        )
        task = await session.get(TaskModel, resolved_task_id)
        if task is not None:
            _reset_task_verdict(task)

    return {
        "s3_prefixes": s3_prefixes,
        "deleted": {
            "task_id": task_id,
            "experiment_id": experiment_id,
            "trials_deleted": len(scoped_trial_ids),
            "task_removed": task_removed,
        },
    }


async def delete_experiment_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    org_id: str | None = None,
) -> dict:
    """Delete an experiment, its trials, and any now-orphaned tasks.

    Only trials whose ``experiment_id`` matches this experiment are
    removed. Tasks linked to this experiment via ``task_experiments``
    have that link removed (CASCADE does this when the experiment row is
    dropped below); any task left without trials or other experiment
    links is then deleted outright.
    """
    from oddish.db import task_experiments
    from oddish.db.storage import collect_s3_prefixes_for_deletion

    exp_query = select(ExperimentModel).where(ExperimentModel.id == experiment_id)
    if org_id is not None:
        exp_query = exp_query.where(ExperimentModel.org_id == org_id)

    exp_result = await session.execute(exp_query)
    experiment = exp_result.scalar_one_or_none()
    if not experiment:
        raise HTTPException(
            status_code=404, detail=f"Experiment {experiment_id} not found"
        )

    # Tasks linked to this experiment — snapshot them now so we can check
    # which ones orphan out after the scoped trial delete + link drop.
    linked_task_rows = (
        await session.execute(
            select(TaskModel.id, TaskModel.task_s3_key, TaskModel.task_path)
            .join(
                task_experiments,
                task_experiments.c.task_id == TaskModel.id,
            )
            .where(task_experiments.c.experiment_id == experiment_id)
        )
    ).all()
    linked_task_ids = [row[0] for row in linked_task_rows]
    linked_task_s3 = {row[0]: (row[1], row[2]) for row in linked_task_rows}

    # Trials scoped to this experiment.
    trial_where = [TrialModel.experiment_id == experiment_id]
    if org_id is not None:
        trial_where.append(
            or_(TrialModel.org_id == org_id, TrialModel.org_id.is_(None))
        )

    scoped_trial_rows = (
        await session.execute(
            select(TrialModel.id, TrialModel.trial_s3_key).where(*trial_where)
        )
    ).all()
    scoped_trial_ids = [row[0] for row in scoped_trial_rows]

    # Cancel any live worker_jobs for the trials we're about to delete.
    if scoped_trial_ids:
        await session.execute(
            text(
                """
                UPDATE worker_jobs
                SET    status = 'CANCELLED',
                       finished_at = NOW(),
                       error_message = 'Experiment deleted by user',
                       current_worker_id = NULL,
                       current_queue_slot = NULL,
                       modal_function_call_id = NULL
                WHERE  subject_table = 'trials'
                  AND  subject_id = ANY(:trial_ids)
                  AND  status::text IN ('QUEUED', 'RETRYING', 'RUNNING', 'BLOCKED')
                """
            ),
            {"trial_ids": scoped_trial_ids},
        )

        trials_del = await session.execute(
            delete(TrialModel)
            .where(TrialModel.id.in_(scoped_trial_ids))
            .execution_options(synchronize_session=False)
        )
        deleted_trials = int(trials_del.rowcount or 0)
    else:
        deleted_trials = 0

    # Drop the experiment row. CASCADE on ``task_experiments.experiment_id``
    # automatically removes link rows pointing at this experiment.
    experiments_del_query = delete(ExperimentModel).where(
        ExperimentModel.id == experiment_id
    )
    if org_id is not None:
        experiments_del_query = experiments_del_query.where(
            ExperimentModel.org_id == org_id
        )
    experiments_result = await session.execute(experiments_del_query)
    deleted_experiments = int(experiments_result.rowcount or 0)

    # Any of the previously-linked tasks that now have no trials and no
    # other experiment links are orphaned — drop them + their S3 prefix.
    task_s3_to_delete: list[tuple[str | None, str | None]] = []
    deleted_tasks = 0

    for tid in linked_task_ids:
        remaining_trials = int(
            await session.scalar(
                select(func.count(TrialModel.id)).where(TrialModel.task_id == tid)
            )
            or 0
        )
        remaining_links = int(
            await session.scalar(
                select(func.count())
                .select_from(task_experiments)
                .where(task_experiments.c.task_id == tid)
            )
            or 0
        )
        if remaining_trials == 0 and remaining_links == 0:
            await session.execute(
                text(
                    """
                    UPDATE worker_jobs
                    SET    status = 'CANCELLED',
                           finished_at = NOW(),
                           error_message = 'Experiment deleted by user',
                           current_worker_id = NULL,
                           current_queue_slot = NULL,
                           modal_function_call_id = NULL
                    WHERE  subject_table = 'tasks'
                      AND  subject_id = :task_id
                      AND  status::text IN ('QUEUED', 'RETRYING', 'RUNNING', 'BLOCKED')
                    """
                ),
                {"task_id": tid},
            )
            task_del_result = await session.execute(
                delete(TaskModel)
                .where(TaskModel.id == tid)
                .execution_options(synchronize_session=False)
            )
            deleted_tasks += int(task_del_result.rowcount or 0)
            task_s3_to_delete.append(linked_task_s3[tid])
        else:
            task = await session.get(TaskModel, tid)
            if task is not None:
                _reset_task_verdict(task)

    s3_prefixes = collect_s3_prefixes_for_deletion(
        tasks=task_s3_to_delete,
        trials=[(row[0], row[1]) for row in scoped_trial_rows],
    )

    return {
        "s3_prefixes": s3_prefixes,
        "deleted": {
            "trials": deleted_trials,
            "tasks": deleted_tasks,
            "experiments": deleted_experiments,
        },
    }


async def delete_trial_core(
    session: AsyncSession,
    *,
    trial_id: str,
    org_id: str | None = None,
) -> dict:
    """Delete a single trial, cancel its in-flight jobs, and collect its S3 prefix.

    Also invalidates the parent task's cached verdict so stale aggregates from
    the now-deleted trial do not leak into the dashboard.
    """
    trial = await get_trial_for_org_core(session, trial_id=trial_id, org_id=org_id)

    from oddish.db.storage import collect_s3_prefixes_for_deletion

    s3_prefixes = collect_s3_prefixes_for_deletion(
        tasks=[],
        trials=[(trial.id, trial.trial_s3_key)],
    )

    # Cancel any live worker_jobs belonging to this trial (TRIAL runs and
    # ANALYSIS jobs) so workers stop heart-beating and release slots before
    # the domain row disappears underneath them.
    await session.execute(
        text(
            """
            UPDATE worker_jobs
            SET    status = 'CANCELLED',
                   finished_at = NOW(),
                   error_message = 'Trial deleted by user',
                   current_worker_id = NULL,
                   current_queue_slot = NULL,
                   modal_function_call_id = NULL
            WHERE  subject_table = 'trials'
              AND  subject_id = :trial_id
              AND  status::text IN ('QUEUED', 'RETRYING', 'RUNNING', 'BLOCKED')
            """
        ),
        {"trial_id": trial_id},
    )

    task_id = trial.task_id

    await session.execute(
        delete(TrialModel)
        .where(TrialModel.id == trial_id)
        .execution_options(synchronize_session=False)
    )

    # Task aggregates (total/completed/failed) are derived from the remaining
    # trials, but the cached verdict for the task may reference this trial.
    # Clear it so the dashboard doesn't show stale data.
    if task_id:
        task = await session.get(TaskModel, task_id)
        if task is not None:
            _reset_task_verdict(task)

    return {
        "s3_prefixes": s3_prefixes,
        "deleted": {"trial_id": trial_id, "task_id": task_id},
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
    from oddish.core.sweeps import (
        build_trial_specs_from_sweep,
        build_task_submission_from_sweep,
    )
    from oddish.queue import (
        append_trials_to_task,
        create_task,
        get_experiment_by_id_or_name,
        get_or_create_experiment,
    )
    from oddish.core.tasks import resolve_task_storage
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
        primary_experiment = await _primary_experiment_for_task_model(task)
        if submission.experiment_id:
            experiment = await get_experiment_by_id_or_name(session, submission.experiment_id, org_id)
            if not experiment:
                experiment = await get_or_create_experiment(session, submission.experiment_id, org_id)
            new_experiment_id = experiment.id
        elif primary_experiment is not None:
            experiment = primary_experiment
        else:
            # Task was uploaded via ``oddish upload`` (or otherwise
            # landed in the DB without any trials) and therefore has no
            # linked experiment yet. Auto-create one here so the user
            # can run trials against an upload-only task without having
            # to pass ``--experiment`` explicitly -- mirroring plain
            # ``oddish run`` which also auto-generates an experiment
            # when none is supplied.
            from oddish.experiment import generate_experiment_name

            experiment = await get_or_create_experiment(
                session, generate_experiment_name(), org_id
            )
            new_experiment_id = experiment.id

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
        
        fallback_experiment_id = (
            primary_experiment.id if primary_experiment else None
        )
        append_submission = submission.model_copy(
            update={
                "name": task.name,
                "priority": task.priority,
                "experiment_id": new_experiment_id or fallback_experiment_id,
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
            "Upload it first with POST /tasks/upload/init and POST /tasks/upload/complete"
        ),
        local_missing_detail=(
            f"Task {submission.task_id} not found in local storage. "
            "Direct task uploads require S3-backed storage"
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

    experiment = await _primary_experiment_for_task_model(task)

    return task, list(task.trials), False, experiment

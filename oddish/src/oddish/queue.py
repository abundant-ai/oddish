from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import settings
from oddish.db import (
    AnalysisStatus,
    ExperimentModel,
    Priority,
    TaskModel,
    TaskStatus,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    generate_id,
    utcnow,
)
from oddish.db.storage import extract_s3_key_from_path, get_storage_client
from oddish.experiment import generate_experiment_name
from oddish.schemas import TaskSubmission, TrialSpec
from oddish.task_timeouts import validate_task_timeout_config

logger = logging.getLogger(__name__)


# =============================================================================
# Task/Trial Cancellation (user-initiated)
# =============================================================================


async def cancel_task_runs(
    session: AsyncSession,
    task_id: str,
    org_id: str | None = None,
) -> dict:
    """Cancel all in-flight runs for a task without deleting data.

    1. Mark running/queued/retrying trials as FAILED
    2. Return Modal function call IDs so callers can cancel them remotely

    Returns a summary dict with counts and modal_function_call_ids to cancel.
    """
    query = select(TaskModel).where(TaskModel.id == task_id)
    if org_id:
        query = query.where(TaskModel.org_id == org_id)
    result = await session.execute(query)
    task = result.scalar_one_or_none()
    if not task:
        return {"error": "not_found"}

    trial_rows = await session.execute(
        select(TrialModel).where(TrialModel.task_id == task_id)
    )
    trials = list(trial_rows.scalars().all())

    active_statuses = {
        TrialStatus.PENDING,
        TrialStatus.QUEUED,
        TrialStatus.RUNNING,
        TrialStatus.RETRYING,
    }
    active_pipeline_statuses = {
        AnalysisStatus.PENDING,
        AnalysisStatus.QUEUED,
        AnalysisStatus.RUNNING,
    }
    modal_fc_ids: list[str] = []
    trials_cancelled = 0
    now = utcnow()

    for trial in trials:
        if trial.status not in active_statuses:
            if trial.analysis_status in active_pipeline_statuses:
                trial.analysis_status = AnalysisStatus.FAILED
                trial.analysis_error = "Cancelled by user"
                trial.analysis_finished_at = now
            continue
        if trial.modal_function_call_id:
            modal_fc_ids.append(trial.modal_function_call_id)
        trial.status = TrialStatus.FAILED
        trial.error_message = "Cancelled by user"
        trial.finished_at = now
        trial.harbor_stage = "cancelled"
        trial.max_attempts = trial.attempts
        trial.current_worker_id = None
        trial.current_queue_slot = None
        trial.modal_function_call_id = None
        if trial.analysis_status in active_pipeline_statuses:
            trial.analysis_status = AnalysisStatus.FAILED
            trial.analysis_error = "Cancelled by user"
            trial.analysis_finished_at = now
        trials_cancelled += 1

    if task.status in (
        TaskStatus.PENDING,
        TaskStatus.RUNNING,
        TaskStatus.ANALYZING,
        TaskStatus.VERDICT_PENDING,
    ):
        task.status = TaskStatus.FAILED
        task.finished_at = now
    if task.verdict_status in active_pipeline_statuses:
        task.verdict_status = VerdictStatus.FAILED
        task.verdict_error = "Cancelled by user"
        task.verdict_finished_at = now

    await session.flush()

    return {
        "task_id": task_id,
        "trials_cancelled": trials_cancelled,
        "modal_function_call_ids": modal_fc_ids,
    }


# =============================================================================
# Task/Trial Creation
# =============================================================================


async def _get_or_create_experiment(
    session: AsyncSession, name: str, org_id: str | None = None
) -> ExperimentModel:
    """Fetch an experiment by name (and org_id if provided) or create it if missing."""
    if org_id:
        query = select(ExperimentModel).where(
            ExperimentModel.org_id == org_id,
            ExperimentModel.name == name,
        )
    else:
        query = select(ExperimentModel).where(ExperimentModel.name == name)

    result = await session.execute(
        query.order_by(ExperimentModel.created_at.desc()).limit(1)
    )
    existing: ExperimentModel | None = result.scalar_one_or_none()
    if existing:
        return existing

    experiment = ExperimentModel(name=name, org_id=org_id)
    session.add(experiment)
    await session.flush()
    return experiment


async def _get_experiment_by_id(
    session: AsyncSession, experiment_id: str, org_id: str | None = None
) -> ExperimentModel | None:
    """Fetch an experiment by ID with optional org scoping."""
    query = select(ExperimentModel).where(ExperimentModel.id == experiment_id)
    if org_id:
        query = query.where(ExperimentModel.org_id == org_id)
    result = await session.execute(query)
    return result.scalar_one_or_none()


async def _get_experiment_by_id_or_name(
    session: AsyncSession, experiment_id_or_name: str, org_id: str | None = None
) -> ExperimentModel | None:
    """Fetch an experiment by ID or name with optional org scoping."""
    experiment = await _get_experiment_by_id(session, experiment_id_or_name, org_id)
    if experiment:
        return experiment

    query = select(ExperimentModel).where(ExperimentModel.name == experiment_id_or_name)
    if org_id:
        query = query.where(ExperimentModel.org_id == org_id)
    result = await session.execute(
        query.order_by(ExperimentModel.created_at.desc()).limit(1)
    )
    return result.scalar_one_or_none()


def _derive_task_name(task_path: str, task_id: str | None = None) -> str:
    """Derive a human-readable task name from task_path or task_id."""
    import re

    name = task_path.replace("s3://", "").rstrip("/")

    parts = name.split("/")
    name = parts[-1] if parts else name

    if name == "tasks" and len(parts) > 1:
        name = parts[-2]

    if task_id and name == task_id:
        cleaned = re.sub(r"-[0-9a-f]{8}$", "", name, flags=re.IGNORECASE)
        if cleaned and cleaned != name:
            return cleaned

    return name


def _build_harbor_config_for_trial(
    submission: TaskSubmission,
    spec: TrialSpec,
) -> dict[str, Any] | None:
    """Build the harbor_config JSONB payload for a single trial row."""
    base = submission.harbor.model_dump(mode="json", exclude_defaults=True)

    agent_config_payload: dict[str, Any] = {}
    if spec.agent_config:
        agent_config_payload = spec.agent_config.model_dump(
            mode="json", exclude_defaults=True
        )
        agent_config_payload.pop("name", None)
        agent_config_payload.pop("model_name", None)

    if agent_config_payload:
        base["agent_config"] = agent_config_payload

    return base or None


def _get_next_trial_index(task_id: str, existing_trials: list[TrialModel]) -> int:
    """Return the next numeric suffix for ``{task_id}-{index}`` trial IDs."""
    prefix = f"{task_id}-"
    max_index = -1

    for trial in existing_trials:
        if not trial.id.startswith(prefix):
            continue
        suffix = trial.id[len(prefix) :]
        if suffix.isdigit():
            max_index = max(max_index, int(suffix))

    if max_index >= 0:
        return max_index + 1
    return len(existing_trials)


async def create_task(
    session: AsyncSession,
    submission: TaskSubmission,
    task_id: str | None = None,
    org_id: str | None = None,
) -> TaskModel:
    """Create a task with its trials.

    Trials are created with status=QUEUED which makes them immediately
    visible to the fair-scheduling claim query in workers.
    """
    if task_id is None:
        task_id = generate_id()

    task_name = submission.name or _derive_task_name(submission.task_path, task_id)

    task_path = submission.task_path
    task_s3_key = extract_s3_key_from_path(task_path)
    if not task_s3_key and settings.s3_enabled:
        local_path = Path(task_path)
        if local_path.exists() and local_path.is_dir():
            validate_task_timeout_config(local_path)
            storage = get_storage_client()
            task_s3_key = await storage.upload_task_directory(task_id, local_path)
    elif not task_s3_key:
        local_path = Path(task_path)
        if local_path.exists() and local_path.is_dir():
            validate_task_timeout_config(local_path)

    if submission.experiment_id:
        experiment = await _get_experiment_by_id_or_name(
            session, submission.experiment_id, org_id
        )
        if not experiment:
            experiment = await _get_or_create_experiment(
                session, submission.experiment_id, org_id
            )
    else:
        experiment_name = generate_experiment_name()
        experiment = await _get_or_create_experiment(session, experiment_name, org_id)

    task = TaskModel(
        id=task_id,
        name=task_name,
        org_id=org_id,
        user=submission.user,
        priority=submission.priority,
        task_path=submission.task_path,
        task_s3_key=task_s3_key,
        experiment_id=experiment.id,
        tags=submission.tags,
        run_analysis=submission.run_analysis,
    )
    session.add(task)

    for i, spec in enumerate(submission.trials):
        model = settings.normalize_trial_model(spec.agent, spec.model)
        provider = settings.get_provider_for_trial(spec.agent, model)
        queue_key = settings.get_queue_key_for_trial(spec.agent, model)
        trial_id = f"{task_id}-{i}"
        trial_name = f"{task_name}-{i}"

        harbor_config = _build_harbor_config_for_trial(submission, spec)

        trial = TrialModel(
            id=trial_id,
            name=trial_name,
            task_id=task_id,
            org_id=org_id,
            agent=spec.agent,
            provider=provider,
            queue_key=queue_key,
            model=model,
            timeout_minutes=spec.timeout_minutes,
            environment=spec.environment,
            harbor_config=harbor_config,
            status=TrialStatus.QUEUED,
        )
        session.add(trial)

    await session.flush()
    await session.refresh(task, attribute_names=["trials"])
    return task


async def append_trials_to_task(
    session: AsyncSession,
    *,
    task: TaskModel,
    submission: TaskSubmission,
) -> list[TrialModel]:
    """Append new queued trials to an existing task."""
    trial_rows = await session.execute(
        select(TrialModel)
        .where(TrialModel.task_id == task.id)
        .order_by(TrialModel.created_at.asc(), TrialModel.id.asc())
    )
    existing_trials = list(trial_rows.scalars().all())
    next_index = _get_next_trial_index(task.id, existing_trials)

    new_trials: list[TrialModel] = []
    for spec in submission.trials:
        model = settings.normalize_trial_model(spec.agent, spec.model)
        provider = settings.get_provider_for_trial(spec.agent, model)
        queue_key = settings.get_queue_key_for_trial(spec.agent, model)
        trial_id = f"{task.id}-{next_index}"
        trial_name = f"{task.name}-{next_index}"

        harbor_config = _build_harbor_config_for_trial(submission, spec)

        trial = TrialModel(
            id=trial_id,
            name=trial_name,
            task_id=task.id,
            org_id=task.org_id,
            agent=spec.agent,
            provider=provider,
            queue_key=queue_key,
            model=model,
            timeout_minutes=spec.timeout_minutes,
            environment=spec.environment,
            harbor_config=harbor_config,
            status=TrialStatus.QUEUED,
        )
        session.add(trial)
        new_trials.append(trial)
        next_index += 1

    if new_trials and task.status in (
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.ANALYZING,
        TaskStatus.VERDICT_PENDING,
    ):
        task.status = TaskStatus.RUNNING
        task.finished_at = None

    if new_trials and task.run_analysis:
        task.verdict = None
        task.verdict_status = None
        task.verdict_error = None
        task.verdict_started_at = None
        task.verdict_finished_at = None

    await session.flush()
    await session.refresh(task, attribute_names=["trials"])
    return new_trials


# =============================================================================
# Stage Transitions
# =============================================================================


async def maybe_start_analysis_stage(session: AsyncSession, trial_id: str) -> bool:
    """Check if all trials for a task are done and transition task status.

    If run_analysis is enabled -> status becomes ANALYZING
    If run_analysis is disabled -> status becomes COMPLETED

    Uses SELECT FOR UPDATE to prevent race conditions.
    """
    trial = await session.get(TrialModel, trial_id)
    if not trial:
        return False

    task_id = trial.task_id

    result = await session.execute(
        select(TaskModel).where(TaskModel.id == task_id).with_for_update()
    )
    task = result.scalar_one_or_none()

    if not task:
        return False

    if task.status not in (TaskStatus.PENDING, TaskStatus.RUNNING):
        return False

    pending_count = await session.scalar(
        select(func.count(TrialModel.id)).where(
            and_(
                TrialModel.task_id == task_id,
                TrialModel.status.in_(
                    [
                        TrialStatus.PENDING,
                        TrialStatus.QUEUED,
                        TrialStatus.RUNNING,
                        TrialStatus.RETRYING,
                    ]
                ),
            )
        )
    )

    if pending_count > 0:
        return False

    if task.run_analysis:
        task.status = TaskStatus.ANALYZING
        await session.flush()

        analysis_pending_count = await session.scalar(
            select(func.count(TrialModel.id)).where(
                and_(
                    TrialModel.task_id == task_id,
                    or_(
                        TrialModel.analysis_status.is_(None),
                        TrialModel.analysis_status.in_(
                            [
                                AnalysisStatus.PENDING,
                                AnalysisStatus.QUEUED,
                                AnalysisStatus.RUNNING,
                            ]
                        ),
                    ),
                )
            )
        )
        if analysis_pending_count == 0:
            task.status = TaskStatus.VERDICT_PENDING
            task.verdict_status = VerdictStatus.QUEUED
    else:
        task.status = TaskStatus.COMPLETED
        task.finished_at = utcnow()

    await session.flush()
    return True


async def maybe_start_verdict_stage(session: AsyncSession, trial_id: str) -> bool:
    """Check if all analyses for a task are done. If so, transition to VERDICT_PENDING.

    Uses SELECT FOR UPDATE to prevent race conditions.
    """
    trial = await session.get(TrialModel, trial_id)
    if not trial:
        return False

    task_id = trial.task_id

    result = await session.execute(
        select(TaskModel).where(TaskModel.id == task_id).with_for_update()
    )
    task = result.scalar_one_or_none()

    if not task:
        return False

    if task.status != TaskStatus.ANALYZING:
        return False

    pending_count = await session.scalar(
        select(func.count(TrialModel.id)).where(
            and_(
                TrialModel.task_id == task_id,
                or_(
                    TrialModel.analysis_status.is_(None),
                    TrialModel.analysis_status.in_(
                        [
                            AnalysisStatus.PENDING,
                            AnalysisStatus.QUEUED,
                            AnalysisStatus.RUNNING,
                        ]
                    ),
                ),
            )
        )
    )

    if pending_count > 0:
        return False

    task.status = TaskStatus.VERDICT_PENDING
    task.verdict_status = VerdictStatus.QUEUED
    await session.flush()

    return True


# =============================================================================
# Query Helpers
# =============================================================================


async def get_task_with_trials(session: AsyncSession, task_id: str) -> TaskModel | None:
    """Get a task with all its trials."""
    result = await session.execute(
        select(TaskModel)
        .options(selectinload(TaskModel.experiment))
        .where(TaskModel.id == task_id)
    )
    return result.scalar_one_or_none()


async def get_queue_stats(session: AsyncSession, org_id: str | None = None) -> dict:
    """Get queue statistics by queue_key across trial/analysis/verdict jobs."""
    stats: dict[str, dict[str, int]] = {}
    valid_statuses = {"pending", "queued", "running", "success", "failed", "retrying"}
    analysis_queue_key = settings.get_analysis_queue_key()
    verdict_queue_key = settings.get_verdict_queue_key()

    def _ensure_queue(queue_key: str) -> None:
        if queue_key not in stats:
            stats[queue_key] = {
                "pending": 0,
                "queued": 0,
                "running": 0,
                "success": 0,
                "failed": 0,
                "retrying": 0,
            }

    def _add(queue_key: str, status_name: str, count: int) -> None:
        resolved_key = settings.normalize_queue_key(queue_key)
        status_key = status_name.lower()
        if status_key not in valid_statuses:
            return
        _ensure_queue(resolved_key)
        stats[resolved_key][status_key] += int(count)

    if org_id:
        result = await session.execute(
            text(
                """
                SELECT COALESCE(queue_key, provider) AS queue_key, status::text AS status, COUNT(*) AS count
                FROM trials
                WHERE org_id = :org_id
                GROUP BY COALESCE(queue_key, provider), status
                """
            ),
            {"org_id": org_id},
        )
    else:
        result = await session.execute(
            text(
                """
                SELECT COALESCE(queue_key, provider) AS queue_key, status::text AS status, COUNT(*) AS count
                FROM trials
                GROUP BY COALESCE(queue_key, provider), status
                """
            )
        )

    for queue_key, status, count in result.all():
        _add(str(queue_key), str(status), int(count))

    analysis_query = (
        select(TrialModel.analysis_status, func.count(TrialModel.id))
        .where(TrialModel.analysis_status.isnot(None))
        .group_by(TrialModel.analysis_status)
    )
    if org_id:
        analysis_query = analysis_query.where(TrialModel.org_id == org_id)
    analysis_result = await session.execute(analysis_query)
    for analysis_status, count in analysis_result.all():
        _add(analysis_queue_key, analysis_status.value, int(count))

    verdict_query = (
        select(TaskModel.verdict_status, func.count(TaskModel.id))
        .where(TaskModel.verdict_status.isnot(None))
        .group_by(TaskModel.verdict_status)
    )
    if org_id:
        verdict_query = verdict_query.where(TaskModel.org_id == org_id)
    verdict_result = await session.execute(verdict_query)
    for verdict_status, count in verdict_result.all():
        _add(verdict_queue_key, verdict_status.value, int(count))

    return stats


async def get_queue_stats_with_concurrency(
    session: AsyncSession, org_id: str | None = None
) -> dict[str, dict]:
    """Get queue stats with recommended concurrency per queue key."""
    stats = await get_queue_stats(session, org_id)
    queue_stats: dict[str, dict] = {}
    queue_keys = set(stats.keys()) | settings.get_known_queue_keys()
    for queue_key in sorted(queue_keys):
        provider_stats = stats.get(
            queue_key,
            {
                "pending": 0,
                "queued": 0,
                "running": 0,
                "success": 0,
                "failed": 0,
                "retrying": 0,
            },
        )
        queue_stats[queue_key] = {
            **provider_stats,
            "recommended_concurrency": settings.get_model_concurrency(queue_key),
        }
    return queue_stats


async def get_pipeline_stats(session: AsyncSession, org_id: str | None = None) -> dict:
    """Get statistics for each pipeline stage."""
    trial_query = select(TrialModel.status, func.count(TrialModel.id)).group_by(
        TrialModel.status
    )
    if org_id:
        trial_query = trial_query.where(TrialModel.org_id == org_id)
    trial_stats = await session.execute(trial_query)
    trials = {status.value: count for status, count in trial_stats.all()}

    analysis_query = (
        select(TrialModel.analysis_status, func.count(TrialModel.id))
        .where(TrialModel.analysis_status.isnot(None))
        .group_by(TrialModel.analysis_status)
    )
    if org_id:
        analysis_query = analysis_query.where(TrialModel.org_id == org_id)
    analysis_stats = await session.execute(analysis_query)
    analyses = {status.value: count for status, count in analysis_stats.all()}

    verdict_query = (
        select(TaskModel.verdict_status, func.count(TaskModel.id))
        .where(TaskModel.verdict_status.isnot(None))
        .group_by(TaskModel.verdict_status)
    )
    if org_id:
        verdict_query = verdict_query.where(TaskModel.org_id == org_id)
    verdict_stats = await session.execute(verdict_query)
    verdicts = {status.value: count for status, count in verdict_stats.all()}

    return {
        "trials": trials,
        "analyses": analyses,
        "verdicts": verdicts,
    }

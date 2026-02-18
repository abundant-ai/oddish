from __future__ import annotations

import json
from pathlib import Path

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
    get_pool,
    utcnow,
)
from pgqueuer.queries import Queries
from pgqueuer.types import JobId
from oddish.db.storage import extract_s3_key_from_path, get_storage_client
from oddish.experiment import generate_experiment_name
from oddish.schemas import TaskSubmission


async def _enqueue_job(
    session: AsyncSession,
    entrypoint: str,
    payload: dict,
    priority: int = 0,
) -> int:
    """Enqueue a job to PGQueuer within the current DB transaction.

    This must run in the same transaction as model inserts to prevent race conditions.
    """
    payload_bytes = json.dumps(payload).encode()

    result = await session.execute(
        text(
            """
            INSERT INTO pgqueuer (priority, entrypoint, payload, status)
            VALUES (:priority, :entrypoint, :payload, 'queued')
            RETURNING id
            """
        ),
        {
            "priority": priority,
            "entrypoint": entrypoint,
            "payload": payload_bytes,
        },
    )
    job_id = result.scalar_one()
    await session.execute(
        text(
            """
            INSERT INTO pgqueuer_log (job_id, status, entrypoint, priority)
            VALUES (:job_id, 'queued', :entrypoint, :priority)
            """
        ),
        {
            "job_id": job_id,
            "entrypoint": entrypoint,
            "priority": priority,
        },
    )
    return job_id


async def enqueue_trial(
    session: AsyncSession,
    trial_id: str,
    provider: str,
    priority: int = 0,
) -> None:
    """Enqueue a trial job to PGQueuer *within the current DB transaction*.

    This must run in the same transaction as `TaskModel`/`TrialModel` inserts.
    Otherwise, a worker can dequeue the PGQueuer job before the trial row is
    committed and permanently "drop" the job (leaving the trial stuck QUEUED).
    """
    await _enqueue_job(
        session,
        entrypoint=provider,
        payload={"job_type": "trial", "trial_id": trial_id},
        priority=priority,
    )


async def enqueue_analysis(
    session: AsyncSession,
    trial_id: str,
    priority: int = 0,
) -> None:
    """Enqueue an analysis job for a trial (always uses claude queue).

    Analysis jobs classify trial outcomes using LLM.
    """
    await _enqueue_job(
        session,
        entrypoint="claude",  # Analysis always uses Claude
        payload={"job_type": "analysis", "trial_id": trial_id},
        priority=priority,
    )


async def enqueue_verdict(
    session: AsyncSession,
    task_id: str,
    priority: int = 0,
) -> None:
    """Enqueue a verdict job for a task (always uses openai queue).

    Verdict jobs synthesize trial classifications into a final task verdict.
    """
    await _enqueue_job(
        session,
        entrypoint="openai",  # Verdict runs on openai provider queue
        payload={"job_type": "verdict", "task_id": task_id},
        priority=priority,
    )


# =============================================================================
# PGQueuer Cleanup
# =============================================================================


async def _cancel_pgqueuer_jobs(
    session: AsyncSession,
    job_ids: list[int],
) -> int:
    if not job_ids:
        return 0

    pool = await get_pool()
    queries = Queries.from_asyncpg_pool(pool)
    await queries.mark_job_as_cancelled([JobId(job_id) for job_id in job_ids])
    return len(job_ids)


async def cancel_pgqueuer_jobs_for_trials(
    session: AsyncSession,
    trial_ids: list[str],
) -> int:
    """Cancel PGQueuer jobs tied to trial IDs (trials + analyses)."""
    if not trial_ids:
        return 0

    result = await session.execute(
        text(
            """
            SELECT id
            FROM pgqueuer
            WHERE payload IS NOT NULL
              AND (convert_from(payload, 'UTF8')::jsonb ->> 'trial_id') = ANY(:trial_ids)
            """
        ),
        {"trial_ids": trial_ids},
    )
    job_ids = [int(row[0]) for row in result.all()]
    return await _cancel_pgqueuer_jobs(session, job_ids)


async def cancel_pgqueuer_jobs_for_tasks(
    session: AsyncSession,
    task_ids: list[str],
) -> int:
    """Cancel PGQueuer jobs tied to task IDs (verdict jobs)."""
    if not task_ids:
        return 0

    result = await session.execute(
        text(
            """
            SELECT id
            FROM pgqueuer
            WHERE payload IS NOT NULL
              AND (convert_from(payload, 'UTF8')::jsonb ->> 'task_id') = ANY(:task_ids)
            """
        ),
        {"task_ids": task_ids},
    )
    job_ids = [int(row[0]) for row in result.all()]
    return await _cancel_pgqueuer_jobs(session, job_ids)


# =============================================================================
# Task/Trial Creation
# =============================================================================


async def _get_or_create_experiment(
    session: AsyncSession, name: str, org_id: str | None = None
) -> ExperimentModel:
    """Fetch an experiment by name (and org_id if provided) or create it if missing."""
    if org_id:
        # Multi-tenant: lookup by (org_id, name)
        query = select(ExperimentModel).where(
            ExperimentModel.org_id == org_id,
            ExperimentModel.name == name,
        )
    else:
        # OSS single-tenant: lookup by name only
        query = select(ExperimentModel).where(ExperimentModel.name == name)

    result = await session.execute(
        query.order_by(ExperimentModel.created_at.desc()).limit(1)
    )
    experiment = result.scalar_one_or_none()
    if experiment:
        return experiment

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
    """Fetch an experiment by ID or name with optional org scoping.

    First tries to match by ID, then falls back to name lookup.
    """
    # Try by ID first
    experiment = await _get_experiment_by_id(session, experiment_id_or_name, org_id)
    if experiment:
        return experiment

    # Fall back to name lookup
    query = select(ExperimentModel).where(ExperimentModel.name == experiment_id_or_name)
    if org_id:
        query = query.where(ExperimentModel.org_id == org_id)
    result = await session.execute(
        query.order_by(ExperimentModel.created_at.desc()).limit(1)
    )
    return result.scalar_one_or_none()


def _derive_task_name(task_path: str, task_id: str | None = None) -> str:
    """Derive a human-readable task name from task_path or task_id.

    Strips common prefixes (s3://, tasks/) and UUID/hash suffixes.
    """
    import re

    # Remove s3:// prefix and trailing slashes
    name = task_path.replace("s3://", "").rstrip("/")

    # Get the last path component
    parts = name.split("/")
    name = parts[-1] if parts else name

    # Strip "tasks" prefix if that's all we have
    if name == "tasks" and len(parts) > 1:
        name = parts[-2]

    # If the name looks like it's just the task_id (from s3://tasks/<id>/),
    # try to strip the UUID suffix to get the clean name
    if task_id and name == task_id:
        # Strip 8-char hex suffix (e.g., "axios-12345678" -> "axios")
        cleaned = re.sub(r"-[0-9a-f]{8}$", "", name, flags=re.IGNORECASE)
        if cleaned and cleaned != name:
            return cleaned

    return name


async def create_task(
    session: AsyncSession,
    submission: TaskSubmission,
    task_id: str | None = None,
    org_id: str | None = None,
) -> TaskModel:
    """Create a task with its trials and enqueue them to PGQueuer.

    Args:
        session: Database session
        submission: Task submission data
        task_id: Optional custom task ID
        org_id: Optional organization ID for multi-tenant deployments.
                When provided, propagates to experiment and all trials.
    """
    if task_id is None:
        task_id = generate_id()

    # Derive task name (use explicit name if provided, otherwise derive from path)
    task_name = submission.name or _derive_task_name(submission.task_path, task_id)

    # Handle task storage based on path format
    task_path = submission.task_path
    task_s3_key = extract_s3_key_from_path(task_path)
    if not task_s3_key and settings.s3_enabled:
        # Legacy: upload local directory to S3
        local_path = Path(task_path)
        if local_path.exists() and local_path.is_dir():
            storage = get_storage_client()
            task_s3_key = await storage.upload_task_directory(task_id, local_path)

    # Resolve experiment by ID or name (creates if not found)
    if submission.experiment_id:
        # First try to find by ID or name
        experiment = await _get_experiment_by_id_or_name(
            session, submission.experiment_id, org_id
        )
        if not experiment:
            # Not found - create with the given name
            experiment = await _get_or_create_experiment(
                session, submission.experiment_id, org_id
            )
    else:
        experiment_name = generate_experiment_name()
        experiment = await _get_or_create_experiment(session, experiment_name, org_id)

    # Create task
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

    # Priority for PGQueuer (higher number = higher priority)
    pgq_priority = 1000 if submission.priority == Priority.HIGH else 0

    # Build harbor passthrough config from submission-level defaults
    base_harbor_config: dict = {}
    if submission.disable_verification:
        base_harbor_config["disable_verification"] = True
    if submission.verifier_timeout_sec is not None:
        base_harbor_config["verifier_timeout_sec"] = submission.verifier_timeout_sec
    if submission.env_cpus is not None:
        base_harbor_config["env_cpus"] = submission.env_cpus
    if submission.env_memory_mb is not None:
        base_harbor_config["env_memory_mb"] = submission.env_memory_mb
    if submission.env_storage_mb is not None:
        base_harbor_config["env_storage_mb"] = submission.env_storage_mb
    if submission.env_gpus is not None:
        base_harbor_config["env_gpus"] = submission.env_gpus
    if submission.env_gpu_types is not None:
        base_harbor_config["env_gpu_types"] = submission.env_gpu_types
    if submission.allow_internet is not None:
        base_harbor_config["allow_internet"] = submission.allow_internet
    if submission.agent_setup_timeout_sec is not None:
        base_harbor_config["agent_setup_timeout_sec"] = (
            submission.agent_setup_timeout_sec
        )
    if submission.docker_image is not None:
        base_harbor_config["docker_image"] = submission.docker_image
    if submission.mcp_servers is not None:
        base_harbor_config["mcp_servers"] = [
            s.model_dump() for s in submission.mcp_servers
        ]
    if submission.artifacts is not None:
        base_harbor_config["artifacts"] = [
            a.model_dump() if hasattr(a, "model_dump") else a
            for a in submission.artifacts
        ]
    # Modal sandbox lifecycle
    if submission.sandbox_timeout_secs is not None:
        base_harbor_config["sandbox_timeout_secs"] = submission.sandbox_timeout_secs
    if submission.sandbox_idle_timeout_secs is not None:
        base_harbor_config["sandbox_idle_timeout_secs"] = (
            submission.sandbox_idle_timeout_secs
        )
    # Daytona sandbox lifecycle
    if submission.auto_stop_interval_mins is not None:
        base_harbor_config["auto_stop_interval_mins"] = (
            submission.auto_stop_interval_mins
        )
    if submission.auto_delete_interval_mins is not None:
        base_harbor_config["auto_delete_interval_mins"] = (
            submission.auto_delete_interval_mins
        )
    if submission.snapshot_template_name is not None:
        base_harbor_config["snapshot_template_name"] = submission.snapshot_template_name

    # Create trials
    trials_to_enqueue: list[tuple[str, str]] = []  # (trial_id, provider)
    for i, spec in enumerate(submission.trials):
        provider = settings.get_provider_for_trial(spec.agent, spec.model)
        trial_id = f"{task_id}-{i}"
        trial_name = f"{task_name}-{i}"

        # Merge submission-level config with per-trial agent config
        harbor_config = base_harbor_config.copy()
        if spec.agent_env:
            harbor_config["agent_env"] = spec.agent_env
        if spec.agent_kwargs:
            harbor_config["agent_kwargs"] = spec.agent_kwargs

        trial = TrialModel(
            id=trial_id,
            name=trial_name,
            task_id=task_id,
            org_id=org_id,  # Denormalized for efficient org-scoped queries
            agent=spec.agent,
            provider=provider,
            model=spec.model,
            timeout_minutes=spec.timeout_minutes,
            environment=spec.environment,
            harbor_config=harbor_config or None,
            status=TrialStatus.QUEUED,  # Mark as queued immediately
        )
        session.add(trial)
        trials_to_enqueue.append((trial_id, provider))

    await session.flush()

    # Enqueue all trials to PGQueuer
    for trial_id, provider in trials_to_enqueue:
        await enqueue_trial(session, trial_id, provider, priority=pgq_priority)

    # Refresh to load the trials relationship
    await session.refresh(task, attribute_names=["trials"])
    return task


# =============================================================================
# Stage Transitions
# =============================================================================


async def maybe_start_analysis_stage(session: AsyncSession, trial_id: str) -> bool:
    """
    Check if all trials for a task are done and transition task status.

    If run_analysis is enabled → status becomes ANALYZING (analysis jobs already enqueued per-trial)
    If run_analysis is disabled → status becomes COMPLETED

    Uses SELECT FOR UPDATE to prevent race conditions when multiple trials
    complete simultaneously.

    Returns True if task transitioned to next stage.
    """
    trial = await session.get(TrialModel, trial_id)
    if not trial:
        return False

    task_id = trial.task_id

    # Lock the task row to prevent concurrent updates
    result = await session.execute(
        select(TaskModel).where(TaskModel.id == task_id).with_for_update()
    )
    task = result.scalar_one_or_none()

    if not task:
        return False

    # If task has already moved past RUNNING, another trial beat us to it
    if task.status not in (TaskStatus.PENDING, TaskStatus.RUNNING):
        return False

    # Check if any trials are still pending/queued/running
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

    # All trials done - transition task status
    if task.run_analysis:
        # Analysis jobs were enqueued per-trial in run_trial_job
        task.status = TaskStatus.ANALYZING
        await session.flush()

        # If analyses already finished before we reached ANALYZING,
        # enqueue verdict immediately to avoid getting stuck.
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
            await enqueue_verdict(session, task_id)
    else:
        task.status = TaskStatus.COMPLETED
        task.finished_at = utcnow()

    await session.flush()
    return True


async def maybe_start_verdict_stage(session: AsyncSession, trial_id: str) -> bool:
    """
    Check if all analyses for a task are done. If so, transition to VERDICT_PENDING
    and enqueue the verdict job.

    Uses SELECT FOR UPDATE to prevent race conditions when multiple analyses
    complete simultaneously.

    Returns True if task transitioned to verdict stage.
    """
    # Get task_id from trial
    trial = await session.get(TrialModel, trial_id)
    if not trial:
        return False

    task_id = trial.task_id

    # Lock the task row to prevent concurrent updates
    result = await session.execute(
        select(TaskModel).where(TaskModel.id == task_id).with_for_update()
    )
    task = result.scalar_one_or_none()

    if not task:
        return False

    # If task has already moved past ANALYZING, another analysis beat us to it
    if task.status != TaskStatus.ANALYZING:
        return False

    # Check if any analyses are still pending/queued/running
    pending_count = await session.scalar(
        select(func.count(TrialModel.id)).where(
            and_(
                TrialModel.task_id == task_id,
                TrialModel.analysis_status.in_(
                    [
                        AnalysisStatus.PENDING,
                        AnalysisStatus.QUEUED,
                        AnalysisStatus.RUNNING,
                    ]
                ),
            )
        )
    )

    if pending_count > 0:
        return False

    # All analyses done - transition to VERDICT_PENDING and enqueue verdict job
    task.status = TaskStatus.VERDICT_PENDING
    task.verdict_status = VerdictStatus.QUEUED
    await enqueue_verdict(session, task_id)
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
    """Get queue statistics by provider from the trials table.

    The trials table is the source of truth for trial status, providing
    a complete view of all trials including historical data.

    Args:
        session: Database session
        org_id: Optional organization ID for multi-tenant filtering
    """
    stats: dict[str, dict[str, int]] = {}
    valid_statuses = {"pending", "queued", "running", "success", "failed", "retrying"}

    if org_id:
        result = await session.execute(
            text(
                """
                SELECT provider, status::text AS status, COUNT(*) AS count
                FROM trials
                WHERE org_id = :org_id
                GROUP BY provider, status
                """
            ),
            {"org_id": org_id},
        )
    else:
        result = await session.execute(
            text(
                """
                SELECT provider, status::text AS status, COUNT(*) AS count
                FROM trials
                GROUP BY provider, status
                """
            )
        )

    for provider, status, count in result.all():
        provider_name = str(provider).lower()
        status_str = str(status).lower()
        if status_str not in valid_statuses:
            continue

        if provider_name not in stats:
            stats[provider_name] = {
                "pending": 0,
                "queued": 0,
                "running": 0,
                "success": 0,
                "failed": 0,
                "retrying": 0,
            }
        stats[provider_name][status_str] += count

    return stats


async def get_queue_stats_with_concurrency(
    session: AsyncSession, org_id: str | None = None
) -> dict[str, dict]:
    """Get queue stats with recommended concurrency per provider."""
    stats = await get_queue_stats(session, org_id)
    queue_stats: dict[str, dict] = {}
    for provider in settings.default_provider_concurrency:
        provider_stats = stats.get(
            provider,
            {
                "pending": 0,
                "queued": 0,
                "running": 0,
                "success": 0,
                "failed": 0,
                "retrying": 0,
            },
        )
        queue_stats[provider] = {
            **provider_stats,
            "recommended_concurrency": settings.get_default_concurrency_for_provider(
                provider
            ),
        }
    return queue_stats


async def get_pipeline_stats(session: AsyncSession, org_id: str | None = None) -> dict:
    """Get statistics for each pipeline stage.

    Args:
        session: Database session
        org_id: Optional organization ID for multi-tenant filtering
    """
    # Trials - count by status
    trial_query = select(TrialModel.status, func.count(TrialModel.id)).group_by(
        TrialModel.status
    )
    if org_id:
        trial_query = trial_query.where(TrialModel.org_id == org_id)
    trial_stats = await session.execute(trial_query)
    trials = {status.value: count for status, count in trial_stats.all()}

    # Analyses (from trial.analysis_status field)
    analysis_query = (
        select(TrialModel.analysis_status, func.count(TrialModel.id))
        .where(TrialModel.analysis_status.isnot(None))
        .group_by(TrialModel.analysis_status)
    )
    if org_id:
        analysis_query = analysis_query.where(TrialModel.org_id == org_id)
    analysis_stats = await session.execute(analysis_query)
    analyses = {status.value: count for status, count in analysis_stats.all()}

    # Verdicts (from task.verdict_status field)
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

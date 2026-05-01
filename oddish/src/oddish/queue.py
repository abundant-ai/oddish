from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from dataclasses import dataclass

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import settings
from oddish.core.agent_identity import compute_agent_equivalence_key
from oddish.core.jobs import (
    AgentCellSpec,
    add_experiment_cells,
    add_job_cells,
    create_batch_job,
)
from oddish.db import (
    AnalysisStatus,
    BatchJobKind,
    BatchJobStatus,
    ExperimentModel,
    TaskModel,
    TaskStatus,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    WorkerJobKind,
    WorkerJobModel,
    generate_id,
    utcnow,
)
from oddish.db.storage import extract_s3_key_from_path, get_storage_client
from oddish.experiment import generate_experiment_name
from oddish.schemas import TaskSubmission, TrialSpec
from oddish.task_timeouts import validate_task_timeout_config
from oddish.workers.jobs.enqueue import EnqueueRequest, enqueue_worker_job

logger = logging.getLogger(__name__)

USER_CANCELLED_MESSAGE = "Cancelled by user"
CANCELLED_HARBOR_STAGE = "cancelled"
ACTIVE_TRIAL_STATUSES = (
    TrialStatus.PENDING,
    TrialStatus.QUEUED,
    TrialStatus.RUNNING,
    TrialStatus.RETRYING,
)
ACTIVE_PIPELINE_STATUSES = (
    AnalysisStatus.PENDING,
    AnalysisStatus.QUEUED,
    AnalysisStatus.RUNNING,
)
ACTIVE_TASK_STATUSES = (
    TaskStatus.PENDING,
    TaskStatus.RUNNING,
    TaskStatus.ANALYZING,
    TaskStatus.VERDICT_PENDING,
)


@dataclass(frozen=True)
class PlannedTrial:
    id: str
    provider: str
    job_id: str


# =============================================================================
# Task/Trial Cancellation (user-initiated)
# =============================================================================


async def cancel_tasks_runs(
    session: AsyncSession,
    task_ids: list[str],
    org_id: str | None = None,
) -> dict:
    """Cancel in-flight runs for a batch of tasks without deleting data.

    The cancel path walks ``worker_jobs`` (single UPDATE covers trial /
    analysis / verdict kinds uniformly) and then mirrors the terminal
    state back onto the domain rows for live-UI visibility. Modal
    function call ids are harvested from the cancelled rows so callers
    can terminate the remote containers.
    """
    requested_task_ids = list(dict.fromkeys(task_ids))
    if not requested_task_ids:
        return {
            "task_ids": [],
            "not_found_task_ids": [],
            "tasks_found": 0,
            "tasks_cancelled": 0,
            "trials_cancelled": 0,
            "modal_function_call_ids": [],
        }

    query = select(TaskModel).where(TaskModel.id.in_(requested_task_ids))
    if org_id:
        query = query.where(TaskModel.org_id == org_id)
    result = await session.execute(query)
    tasks = list(result.scalars().all())
    if not tasks:
        return {"error": "not_found"}

    tasks_by_id = {task.id: task for task in tasks}
    found_task_ids = [
        task_id for task_id in requested_task_ids if task_id in tasks_by_id
    ]
    not_found_task_ids = [
        task_id for task_id in requested_task_ids if task_id not in tasks_by_id
    ]

    trial_rows = await session.execute(
        select(TrialModel).where(TrialModel.task_id.in_(found_task_ids))
    )
    trials = list(trial_rows.scalars().all())
    trial_ids = [trial.id for trial in trials]

    now = utcnow()

    # Cancel every active worker_jobs row belonging to these trials /
    # tasks. One UPDATE, every kind. Returning the canceled rows gives
    # us the Modal function-call ids to terminate remotely and the kind
    # breakdown for the domain-mirror pass below.
    canceled_rows = (
        (
            await session.execute(
                text(
                    """
                UPDATE worker_jobs
                SET    status = 'CANCELLED',
                       finished_at = NOW(),
                       error_message = :cancel_msg,
                       current_worker_id = NULL,
                       current_queue_slot = NULL,
                       modal_function_call_id = NULL
                WHERE  status::text IN ('QUEUED', 'RETRYING', 'RUNNING', 'BLOCKED')
                  AND  (
                      (subject_table = 'tasks' AND subject_id = ANY(:task_ids))
                      OR (subject_table = 'trials' AND subject_id = ANY(:trial_ids))
                      OR (kind::text = 'TRIAL' AND payload->>'task_id' = ANY(:task_ids))
                  )
                RETURNING id,
                          kind::text AS kind,
                          subject_id,
                          modal_function_call_id
                """
                ),
                {
                    "cancel_msg": USER_CANCELLED_MESSAGE,
                    "task_ids": found_task_ids,
                    "trial_ids": trial_ids or [""],
                },
            )
        )
        .mappings()
        .all()
    )

    modal_fc_ids: list[str] = []
    canceled_trial_kinds: set[str] = set()
    canceled_verdict_task_ids: set[str] = set()
    canceled_analysis_trial_ids: set[str] = set()

    for row in canceled_rows:
        fc = row.get("modal_function_call_id")
        if fc:
            modal_fc_ids.append(str(fc))
        kind = row["kind"]
        subject_id = row["subject_id"]
        if kind == "TRIAL" and subject_id:
            canceled_trial_kinds.add(str(subject_id))
        elif kind == "ANALYSIS" and subject_id:
            canceled_analysis_trial_ids.add(str(subject_id))
        elif kind == "VERDICT" and subject_id:
            canceled_verdict_task_ids.add(str(subject_id))

    # Mirror terminal state back to the domain rows so the dashboard
    # sees "FAILED / Cancelled by user" even before handlers exit.
    trials_cancelled = 0
    for trial in trials:
        trial_updated = False
        if trial.id in canceled_trial_kinds or trial.status in ACTIVE_TRIAL_STATUSES:
            # Modal function-call ids now live only on ``worker_jobs``;
            # the ``UPDATE worker_jobs ... RETURNING`` above is the
            # single source for FCs to terminate.
            trial.status = TrialStatus.FAILED
            trial.error_message = USER_CANCELLED_MESSAGE
            trial.finished_at = now
            trial.harbor_stage = CANCELLED_HARBOR_STAGE
            # max_attempts==attempts is the legacy signal the runtime
            # uses to recognise "cancelled by user" during late-arriving
            # Harbor hooks; preserve that contract.
            trial.max_attempts = trial.attempts
            trial.current_worker_id = None
            trial.current_queue_slot = None
            trials_cancelled += 1
            trial_updated = True
        if (
            trial.id in canceled_analysis_trial_ids
            or trial.analysis_status in ACTIVE_PIPELINE_STATUSES
        ):
            trial.analysis_status = AnalysisStatus.FAILED
            trial.analysis_error = USER_CANCELLED_MESSAGE
            trial.analysis_finished_at = now
            trial_updated = True
        if not trial_updated:
            continue

    tasks_cancelled = 0
    for task in tasks:
        task_updated = False
        if task.status in ACTIVE_TASK_STATUSES:
            task.status = TaskStatus.FAILED
            task.finished_at = now
            task_updated = True
        if (
            task.id in canceled_verdict_task_ids
            or task.verdict_status in ACTIVE_PIPELINE_STATUSES
        ):
            task.verdict_status = VerdictStatus.FAILED
            task.verdict_error = USER_CANCELLED_MESSAGE
            task.verdict_finished_at = now
            task_updated = True
        if task_updated:
            tasks_cancelled += 1

    await session.flush()

    return {
        "task_ids": found_task_ids,
        "not_found_task_ids": not_found_task_ids,
        "tasks_found": len(found_task_ids),
        "tasks_cancelled": tasks_cancelled,
        "trials_cancelled": trials_cancelled,
        "modal_function_call_ids": list(dict.fromkeys(modal_fc_ids)),
    }


# =============================================================================
# Task/Trial Creation
# =============================================================================


async def get_or_create_experiment(
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


async def get_experiment_by_id_or_name(
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

    # Skip versioned path segments (e.g. "v1", "v2") produced by
    # resolve_task_storage for the init/complete upload path.
    if re.match(r"^v\d+$", name) and len(parts) > 1:
        name = parts[-2]

    if name == "tasks" and len(parts) > 1:
        name = parts[-2]

    if task_id and name == task_id:
        cleaned = re.sub(r"-[0-9a-f]{8}$", "", name, flags=re.IGNORECASE)
        if cleaned and cleaned != name:
            return cleaned

    return name


# =============================================================================
# Worker-jobs enqueue helpers
# =============================================================================
#
# Every domain-row insertion or stage transition that schedules compute
# work has a sibling ``worker_jobs`` row in the same transaction. The
# dispatcher claims from ``worker_jobs`` only; these helpers are the
# single enqueue surface for the TRIAL / ANALYSIS / VERDICT kinds.


async def enqueue_trial_worker_job(
    session: AsyncSession,
    *,
    trial_id: str,
    queue_key: str,
    org_id: str | None,
    max_attempts: int,
    parent_job_id: str | None = None,
    job_id: str | None = None,
    subject_table: str | None = "trials",
    subject_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> WorkerJobModel:
    effective_payload = {"trial_id": trial_id}
    if payload:
        effective_payload.update(payload)
    return await enqueue_worker_job(
        session,
        EnqueueRequest(
            kind=WorkerJobKind.TRIAL,
            queue_key=queue_key,
            payload=effective_payload,
            subject_table=subject_table,
            subject_id=subject_id or trial_id,
            org_id=org_id,
            max_attempts=max_attempts,
            parent_job_id=parent_job_id,
            job_id=job_id,
        ),
    )


async def enqueue_analysis_worker_job(
    session: AsyncSession,
    *,
    trial_id: str,
    org_id: str | None,
    parent_job_id: str | None = None,
    job_id: str | None = None,
) -> WorkerJobModel:
    return await enqueue_worker_job(
        session,
        EnqueueRequest(
            kind=WorkerJobKind.ANALYSIS,
            queue_key=settings.get_analysis_queue_key(),
            payload={"trial_id": trial_id},
            subject_table="trials",
            subject_id=trial_id,
            org_id=org_id,
            parent_job_id=parent_job_id,
            job_id=job_id,
        ),
    )


async def enqueue_verdict_worker_job(
    session: AsyncSession,
    *,
    task_id: str,
    org_id: str | None,
    job_id: str | None = None,
) -> WorkerJobModel:
    return await enqueue_worker_job(
        session,
        EnqueueRequest(
            kind=WorkerJobKind.VERDICT,
            queue_key=settings.get_verdict_queue_key(),
            payload={"task_id": task_id},
            subject_table="tasks",
            subject_id=task_id,
            org_id=org_id,
            job_id=job_id,
        ),
    )


async def enqueue_task_expand_worker_job(
    session: AsyncSession,
    *,
    task_id: str,
    version: int,
    org_id: str | None,
) -> WorkerJobModel:
    """Schedule a task-version expansion.

    Expansion writes each member of the task's tarball as an individual
    S3 object under ``tasks/{task_id}/v{version}-files/`` plus a
    ``.oddish-manifest.json`` sentinel that records the source archive's
    etag. The handler short-circuits when the manifest already matches
    the current archive, so repeat enqueues are cheap.
    """
    return await enqueue_worker_job(
        session,
        EnqueueRequest(
            kind=WorkerJobKind.TASK_EXPAND,
            queue_key=settings.get_task_expand_queue_key(),
            payload={"task_id": task_id, "version": version},
            subject_table="task_versions",
            subject_id=f"{task_id}-v{version}",
            org_id=org_id,
        ),
    )


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


def _get_next_trial_index_from_ids(task_id: str, existing_ids: list[str]) -> int:
    """Return the next numeric suffix for ``{task_id}-{index}`` trial IDs."""
    prefix = f"{task_id}-"
    max_index = -1

    for trial_id in existing_ids:
        if not trial_id.startswith(prefix):
            continue
        suffix = trial_id[len(prefix) :]
        if suffix.isdigit():
            max_index = max(max_index, int(suffix))

    if max_index >= 0:
        return max_index + 1
    return len(existing_ids)


def _get_next_trial_index(task_id: str, existing_trials: list[TrialModel]) -> int:
    return _get_next_trial_index_from_ids(
        task_id,
        [trial.id for trial in existing_trials],
    )


async def _get_reserved_trial_ids(session: AsyncSession, task_id: str) -> list[str]:
    rows = await session.execute(
        text(
            """
            SELECT payload->>'trial_id' AS trial_id
            FROM worker_jobs
            WHERE kind::text = 'TRIAL'
              AND payload->>'task_id' = :task_id
              AND payload ? 'trial_id'
            """
        ),
        {"task_id": task_id},
    )
    return [str(row.trial_id) for row in rows if row.trial_id]


async def create_task(
    session: AsyncSession,
    submission: TaskSubmission,
    task_id: str | None = None,
    org_id: str | None = None,
) -> TaskModel:
    """Create a task and enqueue its requested trial executions.

    A ``TaskVersionModel`` (v1) is also created to snapshot the task
    content for this first submission.
    """
    if task_id is None:
        task_id = generate_id()

    task_name = submission.name or _derive_task_name(submission.task_path, task_id)

    task_path = submission.task_path
    task_s3_key = extract_s3_key_from_path(task_path)
    if not task_s3_key:
        local_path = Path(task_path)
        if local_path.exists() and local_path.is_dir():
            validate_task_timeout_config(local_path)
            storage = get_storage_client()
            task_s3_key = await storage.upload_task_directory(task_id, local_path)

    experiment: ExperimentModel | None = None
    if submission.experiment_id:
        experiment = await get_experiment_by_id_or_name(
            session, submission.experiment_id, org_id
        )
        if not experiment:
            experiment = await get_or_create_experiment(
                session, submission.experiment_id, org_id
            )

    batch_job = create_batch_job(
        session,
        kind=BatchJobKind.AD_HOC,
        status=BatchJobStatus.RUNNING,
        org_id=org_id,
        triggered_by_experiment_id=experiment.id if experiment else None,
    )

    # Insert the task first (without version pointer to avoid circular FK).
    task = TaskModel(
        id=task_id,
        name=task_name,
        org_id=org_id,
        user=submission.user or "unknown",
        priority=submission.priority,
        task_path=submission.task_path,
        task_s3_key=task_s3_key,
        tags=submission.tags,
        run_analysis=submission.run_analysis,
    )
    session.add(task)
    await session.flush()

    if experiment is not None:
        await _link_task_to_experiment(
            session, task_id=task_id, experiment_id=experiment.id
        )

    # Determine the version: if one was pre-created during upload, use the
    # latest; otherwise create v1 now that the task row exists.
    existing_max = await session.scalar(
        select(func.max(TaskVersionModel.version)).where(
            TaskVersionModel.task_id == task_id
        )
    )

    if existing_max is not None:
        latest_version_row = (
            await session.execute(
                select(TaskVersionModel).where(
                    TaskVersionModel.task_id == task_id,
                    TaskVersionModel.version == existing_max,
                )
            )
        ).scalar_one()
        version_id = latest_version_row.id
    else:
        version_number = 1
        version_id = f"{task_id}-v{version_number}"
        version_row = TaskVersionModel(
            id=version_id,
            task_id=task_id,
            version=version_number,
            task_path=submission.task_path,
            task_s3_key=task_s3_key,
            content_hash=submission.content_hash,
        )
        session.add(version_row)
        await session.flush()

        if settings.tasks_expand_archive and task_s3_key:
            # Brand-new task created via /tasks/sweep: enqueue the
            # expansion so the drawer's first click hits S3 directly.
            # Re-uploads and registration-only uploads enqueue in
            # ``oddish.core.tasks``; this covers the sweep-creates-v1
            # path they don't exercise.
            await enqueue_task_expand_worker_job(
                session,
                task_id=task_id,
                version=version_number,
                org_id=org_id,
            )

    # Now safe to set the back-pointer and enqueue executions. Trial rows are
    # terminal evidence and are inserted by the worker after Harbor finishes.
    task.current_version_id = version_id

    job_cells: list[AgentCellSpec] = []
    prepared: list[tuple[TrialSpec, str, str, str, str, dict[str, Any] | None]] = []
    for spec in submission.trials:
        model = settings.normalize_trial_model(spec.agent, spec.model)
        provider = settings.get_provider_for_trial(spec.agent, model)
        queue_key = settings.get_queue_key_for_trial(spec.agent, model)
        agent_equivalence_key = compute_agent_equivalence_key(
            spec.agent, model, provider
        )
        harbor_config = _build_harbor_config_for_trial(submission, spec)
        prepared.append(
            (spec, model, provider, queue_key, agent_equivalence_key, harbor_config)
        )
        job_cells.append(
            AgentCellSpec(
                task_version_id=version_id,
                harness=spec.agent,
                model=model,
                provider=provider,
            )
        )

    job_cell_rows = add_job_cells(session, job=batch_job, cells=job_cells)
    await add_experiment_cells(
        session,
        experiment_id=experiment.id if experiment else None,
        cells=job_cells,
    )
    planned_trials: list[PlannedTrial] = []
    for i, (spec, model, provider, queue_key, agent_equivalence_key, harbor_config) in enumerate(
        prepared
    ):
        trial_id = f"{task_id}-{i}"
        job_cell = job_cell_rows[(version_id, agent_equivalence_key)]
        await enqueue_trial_worker_job(
            session,
            trial_id=trial_id,
            queue_key=queue_key,
            org_id=org_id,
            max_attempts=6,
            job_id=batch_job.id,
            subject_table="job_cells",
            subject_id=job_cell.id,
            payload={
                "trial_id": trial_id,
                "trial_name": f"{task_name}-{i}",
                "task_id": task_id,
                "task_version_id": version_id,
                "experiment_id": experiment.id if experiment else None,
                "job_id": batch_job.id,
                "job_cell_id": job_cell.id,
                "agent": spec.agent,
                "provider": provider,
                "queue_key": queue_key,
                "model": model,
                "agent_equivalence_key": agent_equivalence_key,
                "timeout_minutes": spec.timeout_minutes,
                "environment": spec.environment,
                "harbor_config": harbor_config,
            },
        )
        planned_trials.append(PlannedTrial(trial_id, provider, batch_job.id))

    await session.flush()
    task._planned_trials = planned_trials  # type: ignore[attr-defined]
    task._job_id = batch_job.id  # type: ignore[attr-defined]
    return task


async def _link_task_to_experiment(
    session: AsyncSession, *, task_id: str, experiment_id: str
) -> None:
    """Insert a ``task_experiments`` association row if missing."""
    from oddish.db import task_experiments

    await session.execute(
        pg_insert(task_experiments)
        .values(task_id=task_id, experiment_id=experiment_id)
        .on_conflict_do_nothing(index_elements=["task_id", "experiment_id"])
    )


async def append_trials_to_task(
    session: AsyncSession,
    *,
    task: TaskModel,
    submission: TaskSubmission,
    experiment_id: str | None = None,
) -> list[PlannedTrial]:
    """Append new trial executions to an existing task.

    New executions are pinned to the task's ``current_version_id``.
    """
    trial_rows = await session.execute(
        select(TrialModel)
        .where(TrialModel.task_id == task.id)
        .order_by(TrialModel.created_at.asc(), TrialModel.id.asc())
    )
    existing_trials = list(trial_rows.scalars().all())
    reserved_trial_ids = await _get_reserved_trial_ids(session, task.id)
    next_index = _get_next_trial_index_from_ids(
        task.id,
        [trial.id for trial in existing_trials] + reserved_trial_ids,
    )

    current_version_id = task.current_version_id
    if current_version_id is None:
        raise ValueError(f"Task {task.id} has no current version; cannot enqueue trials")

    # Pick the target experiment: explicit argument wins, otherwise fall back
    # to the first linked experiment (the task's "primary" association).
    if experiment_id is None:
        primary = list(task.experiments or [])
        trial_experiment_id = primary[0].id if primary else None
    else:
        trial_experiment_id = experiment_id
        await _link_task_to_experiment(
            session, task_id=task.id, experiment_id=experiment_id
        )

    batch_job = create_batch_job(
        session,
        kind=BatchJobKind.AD_HOC,
        status=BatchJobStatus.RUNNING,
        org_id=task.org_id,
        triggered_by_experiment_id=trial_experiment_id,
    )

    job_cells: list[AgentCellSpec] = []
    prepared: list[tuple[TrialSpec, str, str, str, str, dict[str, Any] | None, str]] = []
    for spec in submission.trials:
        model = settings.normalize_trial_model(spec.agent, spec.model)
        provider = settings.get_provider_for_trial(spec.agent, model)
        queue_key = settings.get_queue_key_for_trial(spec.agent, model)
        agent_equivalence_key = compute_agent_equivalence_key(
            spec.agent, model, provider
        )
        trial_id = f"{task.id}-{next_index}"
        harbor_config = _build_harbor_config_for_trial(submission, spec)
        if current_version_id is not None:
            prepared.append(
                (
                    spec,
                    model,
                    provider,
                    queue_key,
                    agent_equivalence_key,
                    harbor_config,
                    trial_id,
                )
            )
            job_cells.append(
                AgentCellSpec(
                    task_version_id=current_version_id,
                    harness=spec.agent,
                    model=model,
                    provider=provider,
                )
            )
        next_index += 1

    job_cell_rows = add_job_cells(session, job=batch_job, cells=job_cells)
    await add_experiment_cells(
        session,
        experiment_id=trial_experiment_id,
        cells=job_cells,
    )
    planned_trials: list[PlannedTrial] = []
    for (
        spec,
        model,
        provider,
        queue_key,
        agent_equivalence_key,
        harbor_config,
        trial_id,
    ) in prepared:
        assert current_version_id is not None
        job_cell = job_cell_rows[(current_version_id, agent_equivalence_key)]
        await enqueue_trial_worker_job(
            session,
            trial_id=trial_id,
            queue_key=queue_key,
            org_id=task.org_id,
            max_attempts=6,
            job_id=batch_job.id,
            subject_table="job_cells",
            subject_id=job_cell.id,
            payload={
                "trial_id": trial_id,
                "trial_name": f"{task.name}-{trial_id.rsplit('-', 1)[-1]}",
                "task_id": task.id,
                "task_version_id": current_version_id,
                "experiment_id": trial_experiment_id,
                "job_id": batch_job.id,
                "job_cell_id": job_cell.id,
                "agent": spec.agent,
                "provider": provider,
                "queue_key": queue_key,
                "model": model,
                "agent_equivalence_key": agent_equivalence_key,
                "timeout_minutes": spec.timeout_minutes,
                "environment": spec.environment,
                "harbor_config": harbor_config,
            },
        )
        planned_trials.append(PlannedTrial(trial_id, provider, batch_job.id))

    if planned_trials and task.status in (
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.ANALYZING,
        TaskStatus.VERDICT_PENDING,
    ):
        task.status = TaskStatus.RUNNING
        task.finished_at = None

    if planned_trials and task.run_analysis:
        task.verdict = None
        task.verdict_status = None
        task.verdict_error = None
        task.verdict_started_at = None
        task.verdict_finished_at = None
        # Cancel any in-flight VERDICT worker_job for this task so a
        # worker that's already claimed (or about to claim) the old
        # row doesn't overwrite the new verdict with stale data.
        # The dispatcher re-enqueues a fresh VERDICT row once all
        # analyses for the new trial set complete.
        await session.execute(
            text(
                """
                UPDATE worker_jobs
                SET    status = 'CANCELLED',
                       finished_at = NOW(),
                       error_message = 'Superseded by appended trials',
                       current_worker_id = NULL,
                       current_queue_slot = NULL,
                       modal_function_call_id = NULL
                WHERE  kind::text = 'VERDICT'
                  AND  subject_table = 'tasks'
                  AND  subject_id = :task_id
                  AND  status::text IN ('QUEUED', 'RETRYING', 'RUNNING', 'BLOCKED')
                """
            ),
            {"task_id": task.id},
        )

    await session.flush()
    task._planned_trials = planned_trials  # type: ignore[attr-defined]
    task._job_id = batch_job.id  # type: ignore[attr-defined]
    return planned_trials


# =============================================================================
# Stage Transitions
# =============================================================================


async def maybe_start_analysis_stage(
    session: AsyncSession,
    trial_id: str,
    *,
    exclude_worker_job_id: str | None = None,
) -> bool:
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
    active_worker_count = await session.scalar(
        text(
            """
            SELECT COUNT(*)
            FROM worker_jobs
            WHERE kind::text = 'TRIAL'
              AND status::text IN ('QUEUED', 'RETRYING', 'RUNNING', 'BLOCKED')
              AND payload->>'task_id' = :task_id
              AND (:exclude_worker_job_id IS NULL OR id <> :exclude_worker_job_id)
            """
        ),
        {"task_id": task_id, "exclude_worker_job_id": exclude_worker_job_id},
    )

    if pending_count > 0 or int(active_worker_count or 0) > 0:
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
            await enqueue_verdict_worker_job(
                session, task_id=task_id, org_id=task.org_id
            )
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
    await enqueue_verdict_worker_job(session, task_id=task_id, org_id=task.org_id)
    await session.flush()

    return True


# =============================================================================
# Query Helpers
# =============================================================================


async def get_task_with_trials(session: AsyncSession, task_id: str) -> TaskModel | None:
    """Get a task with all its trials."""
    result = await session.execute(
        select(TaskModel)
        .options(selectinload(TaskModel.experiments))
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


async def get_queue_and_pipeline_stats_with_concurrency(
    session: AsyncSession, org_id: str | None = None
) -> tuple[dict[str, dict], dict[str, dict[str, int]]]:
    """Collect queue and pipeline stats without duplicating status scans."""
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

    trial_pipeline: dict[str, int] = {}
    analysis_pipeline: dict[str, int] = {}
    verdict_pipeline: dict[str, int] = {}
    analysis_queue_key = settings.get_analysis_queue_key()
    verdict_queue_key = settings.get_verdict_queue_key()

    for queue_key, provider_stats in stats.items():
        for status_name, count in provider_stats.items():
            if queue_key == analysis_queue_key:
                analysis_pipeline[status_name] = analysis_pipeline.get(
                    status_name, 0
                ) + int(count)
            elif queue_key == verdict_queue_key:
                verdict_pipeline[status_name] = verdict_pipeline.get(
                    status_name, 0
                ) + int(count)
            else:
                trial_pipeline[status_name] = trial_pipeline.get(status_name, 0) + int(
                    count
                )

    return queue_stats, {
        "trials": trial_pipeline,
        "analyses": analysis_pipeline,
        "verdicts": verdict_pipeline,
    }


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

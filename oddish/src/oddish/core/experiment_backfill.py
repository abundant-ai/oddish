"""Backfill an experiment by enqueuing trials for cells with gaps.

A backfill is the canonical "run my experiment" action under the
task-first model. It does *not* mutate the experiment; it just produces
new trial evidence pinned to the cells' task_version_ids (which may not
be the current version of the underlying task).

One ``Job(kind=experiment_backfill)`` is created per backfill request so
the resulting trials are easy to find and supervise. Trials are enqueued
through the standard ``worker_jobs`` path; they look identical to live
sweep submissions to the rest of the system.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import settings
from oddish.core.agent_identity import compute_agent_equivalence_key
from oddish.core.experiment_cells import resolve_experiment_core
from oddish.db import (
    ExperimentModel,
    JobKind,
    JobModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    generate_id,
)
from oddish.queue import _get_next_trial_index, enqueue_trial_worker_job
from oddish.schemas import ExperimentBackfillResponse


logger = logging.getLogger(__name__)


async def backfill_experiment_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    org_id: str | None,
    user_id: str | None = None,
) -> ExperimentBackfillResponse:
    """Enqueue trials to fill the gaps on every cell of the experiment.

    Returns the Job id and the count of trials enqueued. If there are no
    gaps, the Job is still created (with zero trials) so the action is
    auditable.
    """
    exp = (
        await session.execute(
            select(ExperimentModel).where(
                ExperimentModel.id == experiment_id,
                *([ExperimentModel.org_id == org_id] if org_id is not None else []),
            )
        )
    ).scalar_one_or_none()
    if exp is None:
        raise HTTPException(status_code=404, detail="Experiment not found")

    resolved = await resolve_experiment_core(
        session, experiment_id=experiment_id, org_id=org_id
    )
    gaps = [c for c in resolved.cells if c.gap > 0]

    job = JobModel(
        id=generate_id(),
        kind=JobKind.EXPERIMENT_BACKFILL,
        name=f"backfill: {exp.name}",
        triggered_by_experiment_id=experiment_id,
        launched_by_user_id=user_id,
        org_id=exp.org_id,
    )
    session.add(job)
    await session.flush()

    if not gaps:
        return ExperimentBackfillResponse(job_id=job.id, enqueued_trial_count=0)

    # Resolve every task_version we'll be enqueuing against in one shot
    # so we can pin trials to the right (task_id, task_version_id).
    tv_ids = list({c.task_version_id for c in gaps})
    tv_rows = (
        await session.execute(
            select(TaskVersionModel.id, TaskVersionModel.task_id)
            .where(TaskVersionModel.id.in_(tv_ids))
        )
    ).all()
    tv_to_task: dict[str, str] = {tv: task_id for tv, task_id in tv_rows}

    task_ids = list({tv_to_task[c.task_version_id] for c in gaps if c.task_version_id in tv_to_task})
    task_rows = (
        await session.execute(
            select(TaskModel).where(TaskModel.id.in_(task_ids))
        )
    ).scalars().all()
    tasks_by_id: dict[str, TaskModel] = {t.id: t for t in task_rows}

    # Group existing trials per task to allocate the next index.
    existing_per_task: dict[str, list[TrialModel]] = {}
    if task_ids:
        ex_trials = (
            await session.execute(
                select(TrialModel).where(TrialModel.task_id.in_(task_ids))
            )
        ).scalars().all()
        for tr in ex_trials:
            existing_per_task.setdefault(tr.task_id, []).append(tr)

    enqueued = 0
    for cell in gaps:
        task_id = tv_to_task.get(cell.task_version_id)
        if task_id is None:
            logger.warning(
                "backfill: task_version_id %s has no task; skipping cell %s",
                cell.task_version_id,
                cell.id,
            )
            continue
        task = tasks_by_id.get(task_id)
        if task is None:
            logger.warning(
                "backfill: task %s missing for cell %s; skipping",
                task_id,
                cell.id,
            )
            continue

        agent = cell.agent.harness
        model = settings.normalize_trial_model(agent, cell.agent.model)
        provider = settings.get_provider_for_trial(agent, model)
        queue_key = settings.get_queue_key_for_trial(agent, model)
        equivalence_key = compute_agent_equivalence_key(agent, model, provider)

        primary_experiment_id = (
            list(task.experiments)[0].id if task.experiments else exp.id
        )

        next_index = _get_next_trial_index(
            task.id, existing_per_task.get(task.id, [])
        )

        for _ in range(cell.gap):
            trial_id = f"{task.id}-{next_index}"
            trial_name = f"{task.name}-{next_index}"
            trial = TrialModel(
                id=trial_id,
                name=trial_name,
                task_id=task.id,
                task_version_id=cell.task_version_id,
                experiment_id=primary_experiment_id,
                job_id=job.id,
                agent_equivalence_key=equivalence_key,
                org_id=task.org_id,
                agent=agent,
                provider=provider,
                queue_key=queue_key,
                model=model,
                status=TrialStatus.QUEUED,
            )
            session.add(trial)
            existing_per_task.setdefault(task.id, []).append(trial)
            await enqueue_trial_worker_job(
                session,
                trial_id=trial_id,
                queue_key=queue_key,
                org_id=task.org_id,
                max_attempts=trial.max_attempts,
                user_job_id=job.id,
            )
            enqueued += 1
            next_index += 1

    await session.flush()
    return ExperimentBackfillResponse(
        job_id=job.id, enqueued_trial_count=enqueued
    )

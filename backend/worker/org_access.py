"""Keep unapproved organizations out of hosted workers, including old queue rows."""

from fastapi import HTTPException
from sqlalchemy import select, union

from models import OrganizationModel
from oddish.db import (
    TaskModel,
    TrialModel,
    get_read_session,
    get_session,
    ACTIVE_TRIAL_STATUSES,
    WorkerJobModel,
    WorkerJobStatus,
)
from oddish.core.helpers import terminate_run_harvest
from oddish.queue import cancel_tasks_runs
from oddish.workers.queue.worker_job_dispatcher import get_worker_job_org_queue_counts
from oddish.workers.queue.worker_job_single_job import ClaimedWorkerJob, JobAccessDenied
from org_access import require_execution_org


async def authorize_worker_job(job: ClaimedWorkerJob) -> None:
    try:
        await require_execution_org(job.org_id)
    except HTTPException as exc:
        raise JobAccessDenied(str(exc.detail)) from exc


async def approved_worker_job_counts(queue_keys):
    queued, running = await get_worker_job_org_queue_counts(queue_keys)
    async with get_read_session() as session:
        approved = set(
            (
                await session.scalars(
                    select(OrganizationModel.id).where(
                        OrganizationModel.is_active.is_(True),
                        OrganizationModel.execution_enabled.is_(True),
                    )
                )
            ).all()
        )
    return {key: count for key, count in queued.items() if key[0] in approved}, running


async def cancel_unapproved_runs() -> int:
    # Use the existing cancellation/teardown path so disabling approval also
    # terminates sandboxes and workers launched before this code was deployed.
    approved = select(OrganizationModel.id).where(
        OrganizationModel.is_active.is_(True),
        OrganizationModel.execution_enabled.is_(True),
        OrganizationModel.deleted_at.is_(None),
    )
    # Start from active work once. A correlated task -> jobs -> trials probe
    # repeats a scan of historical jobs for every task when no work is active.
    active_jobs = (
        select(WorkerJobModel.subject_table, WorkerJobModel.subject_id)
        .where(
            WorkerJobModel.status.in_(
                [
                    WorkerJobStatus.QUEUED,
                    WorkerJobStatus.RETRYING,
                    WorkerJobStatus.RUNNING,
                    WorkerJobStatus.BLOCKED,
                ]
            )
        )
        .cte("active_jobs")
    )
    active_tasks = union(
        select(TrialModel.task_id).where(TrialModel.status.in_(ACTIVE_TRIAL_STATUSES)),
        select(TaskModel.id).join(
            active_jobs,
            (active_jobs.c.subject_table == "tasks")
            & (active_jobs.c.subject_id == TaskModel.id),
        ),
        select(TrialModel.task_id).join(
            active_jobs,
            (active_jobs.c.subject_table == "trials")
            & (active_jobs.c.subject_id == TrialModel.id),
        ),
    )
    async with get_session() as session:
        tasks = list(
            (
                await session.scalars(
                    select(TaskModel.id)
                    .where(TaskModel.id.in_(active_tasks))
                    .where(TaskModel.org_id.is_(None) | ~TaskModel.org_id.in_(approved))
                    .limit(100)
                )
            ).all()
        )
        if not tasks:
            return 0
        result = await cancel_tasks_runs(session, tasks)
        await session.commit()
    await terminate_run_harvest(result, strict=True)
    return len(tasks)

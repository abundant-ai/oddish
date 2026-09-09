"""Keep unapproved organizations out of hosted workers, including old queue rows."""

from fastapi import HTTPException
from sqlalchemy import select

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
    async with get_session() as session:
        tasks = list(
            (
                await session.scalars(
                    select(TaskModel.id)
                    .where(
                        select(TrialModel.id)
                        .where(
                            TrialModel.task_id == TaskModel.id,
                            TrialModel.status.in_(ACTIVE_TRIAL_STATUSES),
                        )
                        .exists()
                        | select(WorkerJobModel.id)
                        .where(
                            WorkerJobModel.status.in_(
                                [
                                    WorkerJobStatus.QUEUED,
                                    WorkerJobStatus.RETRYING,
                                    WorkerJobStatus.RUNNING,
                                    WorkerJobStatus.BLOCKED,
                                ]
                            ),
                            (
                                (WorkerJobModel.subject_table == "tasks")
                                & (WorkerJobModel.subject_id == TaskModel.id)
                            )
                            | (
                                (WorkerJobModel.subject_table == "trials")
                                & WorkerJobModel.subject_id.in_(
                                    select(TrialModel.id)
                                    .where(TrialModel.task_id == TaskModel.id)
                                    .correlate(TaskModel)
                                )
                            ),
                        )
                        .exists()
                    )
                    .where(
                        (TaskModel.org_id.is_(None)) | (~TaskModel.org_id.in_(approved))
                    )
                    .distinct()
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

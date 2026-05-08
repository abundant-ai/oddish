"""Read paths for the user-visible Jobs surface.

A Job is the batch of trials produced by a single submission. This
module computes the aggregate trial counts on read so we don't have to
denormalize a status column. Listing a job's trials is a separate query
through the trials router.
"""

from __future__ import annotations

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db import (
    JobModel,
    TrialModel,
    TrialStatus,
)
from oddish.schemas import JobListResponse, JobResponse


_RUNNING_STATUSES = (
    TrialStatus.QUEUED,
    TrialStatus.RUNNING,
    TrialStatus.RETRYING,
    TrialStatus.PENDING,
)


def _job_row_to_response(
    job: JobModel,
    *,
    trial_count: int = 0,
    succeeded: int = 0,
    failed: int = 0,
    running: int = 0,
) -> JobResponse:
    return JobResponse(
        id=job.id,
        kind=job.kind.value if hasattr(job.kind, "value") else str(job.kind),
        name=job.name,
        triggered_by_experiment_id=job.triggered_by_experiment_id,
        launched_by_user_id=job.launched_by_user_id,
        launched_at=job.launched_at,
        finished_at=job.finished_at,
        org_id=job.org_id,
        trial_count=trial_count,
        succeeded_trial_count=succeeded,
        failed_trial_count=failed,
        running_trial_count=running,
    )


async def list_jobs_core(
    session: AsyncSession,
    *,
    org_id: str | None,
    kind: str | None = None,
    triggered_by_experiment_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JobListResponse:
    """List Jobs with aggregate trial counts.

    Filters: ``kind`` (one of the JobKind values), ``triggered_by_experiment_id``
    (exact match). Results are ordered by ``launched_at`` desc.
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    where = [JobModel.deleted_at.is_(None)]
    if org_id is not None:
        where.append(JobModel.org_id == org_id)
    if kind is not None:
        where.append(JobModel.kind == kind)
    if triggered_by_experiment_id is not None:
        where.append(
            JobModel.triggered_by_experiment_id == triggered_by_experiment_id
        )

    job_rows = (
        await session.execute(
            select(JobModel)
            .where(*where)
            .order_by(JobModel.launched_at.desc(), JobModel.id.desc())
            .limit(limit + 1)
            .offset(offset)
        )
    ).scalars().all()

    has_more = len(job_rows) > limit
    job_rows = list(job_rows[:limit])
    job_ids = [j.id for j in job_rows]

    counts: dict[str, dict[str, int]] = {}
    if job_ids:
        succ_expr = case(
            (TrialModel.status == TrialStatus.SUCCESS, 1), else_=0
        )
        fail_expr = case(
            (TrialModel.status == TrialStatus.FAILED, 1), else_=0
        )
        run_expr = case(
            (TrialModel.status.in_(_RUNNING_STATUSES), 1), else_=0
        )
        agg = await session.execute(
            select(
                TrialModel.job_id,
                func.count().label("total"),
                func.coalesce(func.sum(succ_expr), 0).label("succ"),
                func.coalesce(func.sum(fail_expr), 0).label("fail"),
                func.coalesce(func.sum(run_expr), 0).label("run"),
            )
            .where(TrialModel.job_id.in_(job_ids))
            .group_by(TrialModel.job_id)
        )
        for jid, total, succ, fail, run in agg.all():
            counts[jid] = {
                "total": int(total),
                "succ": int(succ),
                "fail": int(fail),
                "run": int(run),
            }

    items = [
        _job_row_to_response(
            j,
            trial_count=counts.get(j.id, {}).get("total", 0),
            succeeded=counts.get(j.id, {}).get("succ", 0),
            failed=counts.get(j.id, {}).get("fail", 0),
            running=counts.get(j.id, {}).get("run", 0),
        )
        for j in job_rows
    ]
    return JobListResponse(
        items=items, limit=limit, offset=offset, has_more=has_more
    )


async def list_job_trials_core(
    session: AsyncSession,
    *,
    job_id: str,
    org_id: str | None,
    limit: int = 500,
) -> list[dict]:
    """Compact list of trials produced by a Job, for the detail page."""
    where = [JobModel.id == job_id, JobModel.deleted_at.is_(None)]
    if org_id is not None:
        where.append(JobModel.org_id == org_id)
    job = (
        await session.execute(select(JobModel).where(*where))
    ).scalar_one_or_none()
    if job is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Job not found")

    rows = (
        await session.execute(
            select(
                TrialModel.id,
                TrialModel.task_id,
                TrialModel.task_version_id,
                TrialModel.status,
                TrialModel.reward,
                TrialModel.error_message,
                TrialModel.started_at,
                TrialModel.finished_at,
                TrialModel.created_at,
                TrialModel.agent,
                TrialModel.model,
                TrialModel.provider,
            )
            .where(TrialModel.job_id == job_id)
            .order_by(
                TrialModel.created_at.desc(),
                TrialModel.id.desc(),
            )
            .limit(max(1, min(limit, 2000)))
        )
    ).all()
    return [
        {
            "id": r.id,
            "task_id": r.task_id,
            "task_version_id": r.task_version_id,
            "status": r.status.value if hasattr(r.status, "value") else str(r.status),
            "reward": r.reward,
            "error_message": r.error_message,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "agent": r.agent,
            "model": r.model,
            "provider": r.provider,
        }
        for r in rows
    ]


async def get_job_core(
    session: AsyncSession, *, job_id: str, org_id: str | None
) -> JobResponse:
    """Fetch one Job (org-scoped) with aggregate counts."""
    where = [JobModel.id == job_id, JobModel.deleted_at.is_(None)]
    if org_id is not None:
        where.append(JobModel.org_id == org_id)

    job = (
        await session.execute(select(JobModel).where(*where))
    ).scalar_one_or_none()
    if job is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Job not found")

    succ_expr = case((TrialModel.status == TrialStatus.SUCCESS, 1), else_=0)
    fail_expr = case((TrialModel.status == TrialStatus.FAILED, 1), else_=0)
    run_expr = case(
        (TrialModel.status.in_(_RUNNING_STATUSES), 1), else_=0
    )
    row = (
        await session.execute(
            select(
                func.count().label("total"),
                func.coalesce(func.sum(succ_expr), 0).label("succ"),
                func.coalesce(func.sum(fail_expr), 0).label("fail"),
                func.coalesce(func.sum(run_expr), 0).label("run"),
            ).where(TrialModel.job_id == job_id)
        )
    ).one()
    total, succ, fail, run = row

    return _job_row_to_response(
        job,
        trial_count=int(total),
        succeeded=int(succ),
        failed=int(fail),
        running=int(run),
    )

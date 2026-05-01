from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from oddish.core.agent_identity import compute_agent_equivalence_key
from oddish.db import (
    BatchJobKind,
    BatchJobStatus,
    JobCellModel,
    JobModel,
    TrialModel,
    WorkerJobModel,
    WorkerJobStatus,
    generate_id,
    utcnow,
)
from oddish.schemas import JobCellResponse, JobResponse


ACTIVE_WORKER_JOB_STATUSES = {
    WorkerJobStatus.QUEUED,
    WorkerJobStatus.RETRYING,
    WorkerJobStatus.RUNNING,
    WorkerJobStatus.BLOCKED,
}


@dataclass(frozen=True)
class AgentCellSpec:
    task_version_id: str
    harness: str
    model: str
    provider: str
    n_trials: int = 1

    @property
    def agent_equivalence_key(self) -> str:
        return compute_agent_equivalence_key(self.harness, self.model, self.provider)


def create_batch_job(
    session: AsyncSession,
    *,
    kind: BatchJobKind = BatchJobKind.AD_HOC,
    status: BatchJobStatus = BatchJobStatus.RUNNING,
    org_id: str | None = None,
    launched_by_user_id: str | None = None,
    triggered_by_experiment_id: str | None = None,
    finished: bool = False,
) -> JobModel:
    now = utcnow()
    job = JobModel(
        id=generate_id(),
        kind=kind,
        status=status,
        org_id=org_id,
        launched_by_user_id=launched_by_user_id,
        triggered_by_experiment_id=triggered_by_experiment_id,
        launched_at=now,
        finished_at=now if finished else None,
    )
    session.add(job)
    return job


def add_job_cells(
    session: AsyncSession,
    *,
    job: JobModel,
    cells: Iterable[AgentCellSpec],
) -> None:
    merged: dict[tuple[str, str], AgentCellSpec] = {}
    counts: dict[tuple[str, str], int] = {}

    for cell in cells:
        key = (cell.task_version_id, cell.agent_equivalence_key)
        if key not in merged:
            merged[key] = cell
            counts[key] = 0
        counts[key] += cell.n_trials

    for key, cell in merged.items():
        session.add(
            JobCellModel(
                id=generate_id(),
                job_id=job.id,
                task_version_id=cell.task_version_id,
                agent_equivalence_key=cell.agent_equivalence_key,
                harness=cell.harness,
                model=cell.model,
                provider=cell.provider,
                n_trials=counts[key],
            )
        )


@dataclass(frozen=True)
class JobCounts:
    worker_jobs_count: int = 0
    active_worker_jobs_count: int = 0
    failed_worker_jobs_count: int = 0
    cancelled_worker_jobs_count: int = 0
    success_worker_jobs_count: int = 0
    trials_count: int = 0


async def _job_counts(session: AsyncSession, job_ids: list[str]) -> dict[str, JobCounts]:
    if not job_ids:
        return {}

    worker_rows = await session.execute(
        select(
            WorkerJobModel.job_id,
            func.count(WorkerJobModel.id),
            func.count(WorkerJobModel.id).filter(
                WorkerJobModel.status.in_(tuple(ACTIVE_WORKER_JOB_STATUSES))
            ),
            func.count(WorkerJobModel.id).filter(
                WorkerJobModel.status == WorkerJobStatus.FAILED
            ),
            func.count(WorkerJobModel.id).filter(
                WorkerJobModel.status == WorkerJobStatus.CANCELLED
            ),
            func.count(WorkerJobModel.id).filter(
                WorkerJobModel.status == WorkerJobStatus.SUCCESS
            ),
        )
        .where(WorkerJobModel.job_id.in_(job_ids))
        .group_by(WorkerJobModel.job_id)
    )
    mutable_counts: dict[str, list[int]] = {
        str(job_id): [
            int(total),
            int(active),
            int(failed),
            int(cancelled),
            int(success),
            0,
        ]
        for job_id, total, active, failed, cancelled, success in worker_rows.all()
        if job_id is not None
    }

    trial_rows = await session.execute(
        select(TrialModel.job_id, func.count(TrialModel.id))
        .where(TrialModel.job_id.in_(job_ids))
        .group_by(TrialModel.job_id)
    )
    for job_id, trials_count in trial_rows.all():
        if job_id is None:
            continue
        bucket = mutable_counts.setdefault(str(job_id), [0, 0, 0, 0, 0, 0])
        bucket[5] = int(trials_count)

    return {
        job_id: JobCounts(
            worker_jobs_count=vals[0],
            active_worker_jobs_count=vals[1],
            failed_worker_jobs_count=vals[2],
            cancelled_worker_jobs_count=vals[3],
            success_worker_jobs_count=vals[4],
            trials_count=vals[5],
        )
        for job_id, vals in mutable_counts.items()
    }


def _effective_status(job: JobModel, counts: JobCounts) -> BatchJobStatus:
    if counts.active_worker_jobs_count > 0:
        return BatchJobStatus.RUNNING
    if counts.worker_jobs_count == 0:
        return job.status
    if counts.failed_worker_jobs_count > 0:
        return BatchJobStatus.FAILED
    if counts.cancelled_worker_jobs_count > 0:
        return BatchJobStatus.CANCELLED
    if counts.success_worker_jobs_count >= counts.worker_jobs_count:
        return BatchJobStatus.SUCCESS
    return job.status


def _build_job_response(
    job: JobModel,
    *,
    counts: JobCounts,
) -> JobResponse:
    return JobResponse(
        id=job.id,
        kind=job.kind,
        status=_effective_status(job, counts),
        launched_by_user_id=job.launched_by_user_id,
        launched_at=job.launched_at,
        finished_at=job.finished_at,
        triggered_by_experiment_id=job.triggered_by_experiment_id,
        org_id=job.org_id,
        cells=[JobCellResponse.model_validate(cell) for cell in job.cells],
        worker_jobs_count=counts.worker_jobs_count,
        active_worker_jobs_count=counts.active_worker_jobs_count,
        trials_count=counts.trials_count,
        created_at=job.created_at,
    )


async def list_jobs_core(
    session: AsyncSession,
    *,
    org_id: str | None = None,
    status: BatchJobStatus | None = None,
    limit: int = 50,
) -> list[JobResponse]:
    query = select(JobModel).options(selectinload(JobModel.cells))
    if org_id is not None:
        query = query.where(JobModel.org_id == org_id)

    result = await session.execute(
        query.order_by(JobModel.launched_at.desc(), JobModel.created_at.desc()).limit(
            max(limit, 1)
        )
    )
    jobs = list(result.scalars().unique().all())
    counts = await _job_counts(session, [job.id for job in jobs])

    responses = [
        _build_job_response(
            job,
            counts=counts.get(job.id, JobCounts()),
        )
        for job in jobs
    ]
    if status is not None:
        responses = [response for response in responses if response.status == status]
    return responses


async def get_job_core(
    session: AsyncSession,
    *,
    job_id: str,
    org_id: str | None = None,
) -> JobResponse:
    query = (
        select(JobModel)
        .options(selectinload(JobModel.cells))
        .where(JobModel.id == job_id)
    )
    if org_id is not None:
        query = query.where(JobModel.org_id == org_id)
    job = (await session.execute(query)).scalars().unique().one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    counts = (await _job_counts(session, [job.id])).get(job.id, JobCounts())
    return _build_job_response(
        job,
        counts=counts,
    )

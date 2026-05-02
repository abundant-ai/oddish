from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from oddish.core.agent_identity import compute_agent_equivalence_key
from oddish.config import settings
from oddish.db import (
    BatchJobKind,
    BatchJobStatus,
    ExperimentCellModel,
    ExperimentModel,
    JobCellModel,
    JobModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    WorkerJobModel,
    WorkerJobStatus,
    generate_id,
    utcnow,
)
from oddish.schemas import (
    ExperimentCellCreateRequest,
    ExperimentCellResponse,
    ExperimentCreateResponse,
    JobCellResponse,
    JobResponse,
)


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
) -> dict[tuple[str, str], JobCellModel]:
    merged: dict[tuple[str, str], AgentCellSpec] = {}
    counts: dict[tuple[str, str], int] = {}

    for cell in cells:
        key = (cell.task_version_id, cell.agent_equivalence_key)
        if key not in merged:
            merged[key] = cell
            counts[key] = 0
        counts[key] += cell.n_trials

    rows: dict[tuple[str, str], JobCellModel] = {}
    for key, cell in merged.items():
        row = JobCellModel(
            id=generate_id(),
            job_id=job.id,
            task_version_id=cell.task_version_id,
            agent_equivalence_key=cell.agent_equivalence_key,
            harness=cell.harness,
            model=cell.model,
            provider=cell.provider,
            n_trials=counts[key],
        )
        session.add(row)
        rows[key] = row
    return rows


async def add_experiment_cells(
    session: AsyncSession,
    *,
    experiment_id: str | None,
    cells: Iterable[AgentCellSpec],
) -> None:
    if experiment_id is None:
        return

    merged: dict[tuple[str, str], AgentCellSpec] = {}
    counts: dict[tuple[str, str], int] = {}
    for cell in cells:
        key = (cell.task_version_id, cell.agent_equivalence_key)
        if key not in merged:
            merged[key] = cell
            counts[key] = 0
        counts[key] += cell.n_trials

    for key, cell in merged.items():
        stmt = (
            pg_insert(ExperimentCellModel)
            .values(
                id=generate_id(),
                experiment_id=experiment_id,
                task_version_id=cell.task_version_id,
                agent_equivalence_key=cell.agent_equivalence_key,
                harness=cell.harness,
                model=cell.model,
                provider=cell.provider,
                target_n_trials=counts[key],
            )
            .on_conflict_do_update(
                index_elements=[
                    "experiment_id",
                    "task_version_id",
                    "agent_equivalence_key",
                ],
                set_={
                    "target_n_trials": ExperimentCellModel.target_n_trials
                    + counts[key],
                },
            )
        )
        await session.execute(stmt)


async def _require_experiment(
    session: AsyncSession,
    *,
    experiment_id: str,
    org_id: str | None = None,
) -> ExperimentModel:
    query = select(ExperimentModel).where(ExperimentModel.id == experiment_id)
    if org_id is not None:
        query = query.where(ExperimentModel.org_id == org_id)
    experiment = (await session.execute(query)).scalar_one_or_none()
    if experiment is None:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return experiment


async def _build_agent_cell_specs(
    session: AsyncSession,
    *,
    cells: Iterable[ExperimentCellCreateRequest],
    org_id: str | None = None,
) -> list[AgentCellSpec]:
    raw_cells = list(cells)
    if not raw_cells:
        return []

    version_ids = {cell.task_version_id for cell in raw_cells}
    version_query = (
        select(TaskVersionModel.id)
        .join(TaskModel, TaskModel.id == TaskVersionModel.task_id)
        .where(TaskVersionModel.id.in_(version_ids))
    )
    if org_id is not None:
        version_query = version_query.where(TaskModel.org_id == org_id)
    existing_version_ids = set((await session.execute(version_query)).scalars().all())
    missing = sorted(version_ids - existing_version_ids)
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"Task version not found: {missing[0]}",
        )

    specs: list[AgentCellSpec] = []
    for cell in raw_cells:
        harness = cell.agent.harness.strip()
        model = settings.normalize_trial_model(harness, cell.agent.model.strip())
        provider = (cell.agent.provider or "").strip() or settings.get_provider_for_trial(
            harness,
            model,
        )
        specs.append(
            AgentCellSpec(
                task_version_id=cell.task_version_id,
                harness=harness,
                model=model,
                provider=provider,
                n_trials=cell.target_n_trials,
            )
        )
    return specs


async def _upsert_experiment_cells(
    session: AsyncSession,
    *,
    experiment_id: str,
    specs: Iterable[AgentCellSpec],
) -> None:
    for spec in specs:
        stmt = (
            pg_insert(ExperimentCellModel)
            .values(
                id=generate_id(),
                experiment_id=experiment_id,
                task_version_id=spec.task_version_id,
                agent_equivalence_key=spec.agent_equivalence_key,
                harness=spec.harness,
                model=spec.model,
                provider=spec.provider,
                target_n_trials=spec.n_trials,
            )
            .on_conflict_do_update(
                index_elements=[
                    "experiment_id",
                    "task_version_id",
                    "agent_equivalence_key",
                ],
                set_={
                    "harness": spec.harness,
                    "model": spec.model,
                    "provider": spec.provider,
                    "target_n_trials": spec.n_trials,
                },
            )
        )
        await session.execute(stmt)


async def create_experiment_core(
    session: AsyncSession,
    *,
    name: str,
    cells: Iterable[ExperimentCellCreateRequest],
    org_id: str | None = None,
) -> ExperimentCreateResponse:
    normalized_name = name.strip()
    if not normalized_name:
        raise HTTPException(status_code=400, detail="Experiment name cannot be empty")

    specs = await _build_agent_cell_specs(session, cells=cells, org_id=org_id)
    experiment = ExperimentModel(
        id=generate_id(),
        name=normalized_name,
        org_id=org_id,
    )
    session.add(experiment)
    await session.flush()

    await _upsert_experiment_cells(
        session,
        experiment_id=experiment.id,
        specs=specs,
    )
    await session.flush()

    from oddish.core.evidence import get_experiment_cells_core

    return ExperimentCreateResponse(
        id=experiment.id,
        name=experiment.name,
        cells=await get_experiment_cells_core(
            session,
            experiment_id=experiment.id,
            org_id=org_id,
        ),
    )


async def add_experiment_cells_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    cells: Iterable[ExperimentCellCreateRequest],
    org_id: str | None = None,
) -> list[ExperimentCellResponse]:
    await _require_experiment(session, experiment_id=experiment_id, org_id=org_id)
    specs = await _build_agent_cell_specs(session, cells=cells, org_id=org_id)
    await _upsert_experiment_cells(
        session,
        experiment_id=experiment_id,
        specs=specs,
    )
    await session.flush()

    rows = (
        (
            await session.execute(
                select(ExperimentCellModel)
                .where(ExperimentCellModel.experiment_id == experiment_id)
                .order_by(
                    ExperimentCellModel.created_at.desc(),
                    ExperimentCellModel.id.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    return [ExperimentCellResponse.model_validate(row) for row in rows]


async def patch_experiment_cell_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    cell_id: str,
    target_n_trials: int,
    org_id: str | None = None,
) -> ExperimentCellResponse:
    await _require_experiment(session, experiment_id=experiment_id, org_id=org_id)
    cell = await session.get(ExperimentCellModel, cell_id)
    if cell is None or cell.experiment_id != experiment_id:
        raise HTTPException(status_code=404, detail="Experiment cell not found")
    cell.target_n_trials = target_n_trials
    await session.flush()
    return ExperimentCellResponse.model_validate(cell)


async def delete_experiment_cell_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    cell_id: str,
    org_id: str | None = None,
) -> dict[str, str]:
    await _require_experiment(session, experiment_id=experiment_id, org_id=org_id)
    cell = await session.get(ExperimentCellModel, cell_id)
    if cell is None or cell.experiment_id != experiment_id:
        raise HTTPException(status_code=404, detail="Experiment cell not found")
    await session.delete(cell)
    await session.flush()
    return {"status": "deleted", "cell_id": cell_id}


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

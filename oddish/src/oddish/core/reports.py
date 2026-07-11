"""Core CRUD for reports + the experiment-picker options.

Thin domain layer over ReportModel / report_experiments. Generation is kicked
off by enqueueing a REPORT worker job (see workers/queue/report_handler.py).
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db.models import (
    ExperimentModel,
    JobStatus,
    ReportModel,
    report_experiments,
)
from oddish.schemas import ExperimentOption, ReportCreate


async def _enqueue_report_worker_job(session, *, report_id: str, org_id: str | None) -> None:
    # Imported lazily to avoid a core<->queue import cycle at module load.
    from oddish.queue import enqueue_report_worker_job

    await enqueue_report_worker_job(session, report_id=report_id, org_id=org_id)


async def create_report_core(
    session: AsyncSession, *, data: ReportCreate, org_id: str | None, user_id: str | None
) -> ReportModel:
    report = ReportModel(
        name=data.name,
        org_id=org_id,
        owner_user_id=user_id,
        status=JobStatus.PENDING,
    )
    session.add(report)
    await session.flush()  # assigns report.id

    seen: set[str] = set()
    for eid in data.experiment_ids:
        if eid in seen:
            continue
        seen.add(eid)
        await session.execute(
            insert(report_experiments).values(
                report_id=report.id, experiment_id=eid
            )
        )

    await _enqueue_report_worker_job(session, report_id=report.id, org_id=org_id)
    return report


async def get_report_core(
    session: AsyncSession, report_id: str, *, org_id: str | None
) -> ReportModel:
    stmt = select(ReportModel).where(
        ReportModel.id == report_id, ReportModel.org_id == org_id
    )
    report = (await session.execute(stmt)).scalar_one_or_none()
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return report


async def list_reports_core(
    session: AsyncSession, *, org_id: str | None
) -> list[ReportModel]:
    stmt = (
        select(ReportModel)
        .where(ReportModel.org_id == org_id)
        .order_by(ReportModel.created_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def delete_report_core(
    session: AsyncSession, report_id: str, *, org_id: str | None
) -> None:
    from oddish.db.models import utcnow

    report = await get_report_core(session, report_id, org_id=org_id)
    report.deleted_at = utcnow()


async def experiment_ids_for_report(
    session: AsyncSession, report_id: str
) -> list[str]:
    stmt = select(report_experiments.c.experiment_id).where(
        report_experiments.c.report_id == report_id,
        report_experiments.c.deleted_at.is_(None),
    )
    return [row[0] for row in (await session.execute(stmt)).all()]


async def list_experiment_options_core(
    session: AsyncSession, *, org_id: str | None
) -> list[ExperimentOption]:
    stmt = (
        select(ExperimentModel.id, ExperimentModel.name)
        .where(ExperimentModel.org_id == org_id)
        .order_by(ExperimentModel.name)
    )
    rows = (await session.execute(stmt)).all()
    return [ExperimentOption(id=r[0], name=r[1]) for r in rows]

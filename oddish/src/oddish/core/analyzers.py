"""Core CRUD for analyzers + the experiment-picker options.

Thin domain layer over AnalyzerModel / analyzer_experiments. Generation is kicked
off by enqueueing a ANALYZER worker job (see workers/queue/analyzer_handler.py).
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db.models import (
    ExperimentModel,
    JobStatus,
    AnalyzerModel,
    analyzer_experiments,
)
from oddish.schemas import ExperimentOption, AnalyzerCreate


async def _enqueue_analyzer_worker_job(session, *, analyzer_id: str, org_id: str | None) -> None:
    # Imported lazily to avoid a core<->queue import cycle at module load.
    from oddish.queue import enqueue_analyzer_worker_job

    await enqueue_analyzer_worker_job(session, analyzer_id=analyzer_id, org_id=org_id)


async def create_analyzer_core(
    session: AsyncSession, *, data: AnalyzerCreate, org_id: str | None, user_id: str | None
) -> AnalyzerModel:
    seen: set[str] = set(data.experiment_ids)
    requested = list(seen)

    valid_stmt = select(ExperimentModel.id).where(
        ExperimentModel.id.in_(requested), ExperimentModel.org_id == org_id
    )
    valid_ids = {row[0] for row in (await session.execute(valid_stmt)).all()}
    missing = seen - valid_ids
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown or inaccessible experiment ids: {sorted(missing)}",
        )

    analyzer = AnalyzerModel(
        name=data.name,
        org_id=org_id,
        owner_user_id=user_id,
        status=JobStatus.PENDING,
        save_trial_analyses=data.save_trial_analyses,
    )
    session.add(analyzer)
    await session.flush()  # assigns analyzer.id

    for eid in requested:
        await session.execute(
            insert(analyzer_experiments).values(
                analyzer_id=analyzer.id, experiment_id=eid
            )
        )

    await _enqueue_analyzer_worker_job(session, analyzer_id=analyzer.id, org_id=org_id)
    return analyzer


async def get_analyzer_core(
    session: AsyncSession, analyzer_id: str, *, org_id: str | None
) -> AnalyzerModel:
    stmt = select(AnalyzerModel).where(
        AnalyzerModel.id == analyzer_id, AnalyzerModel.org_id == org_id
    )
    analyzer = (await session.execute(stmt)).scalar_one_or_none()
    if analyzer is None:
        raise HTTPException(status_code=404, detail="Analyzer not found")
    return analyzer


async def list_analyzers_core(
    session: AsyncSession, *, org_id: str | None
) -> list[AnalyzerModel]:
    stmt = (
        select(AnalyzerModel)
        .where(AnalyzerModel.org_id == org_id)
        .order_by(AnalyzerModel.created_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def delete_analyzer_core(
    session: AsyncSession, analyzer_id: str, *, org_id: str | None
) -> None:
    from oddish.db.models import utcnow

    analyzer = await get_analyzer_core(session, analyzer_id, org_id=org_id)
    analyzer.deleted_at = utcnow()

    # Cancel any in-flight generation job so it stops burning map/reduce LLM
    # work against a now-deleted analyzer (mirrors task/trial deletion). The
    # running handler's liveness guard then bails without writing back. Terminal
    # rows (SUCCESS/FAILED/CANCELLED) are left untouched.
    await session.execute(
        text(
            """
            UPDATE worker_jobs
            SET    status = 'CANCELLED',
                   finished_at = NOW(),
                   error_message = 'Analyzer deleted',
                   current_worker_id = NULL,
                   current_queue_slot = NULL,
                   modal_function_call_id = NULL
            WHERE  kind::text = 'ANALYZER'
              AND  subject_table = 'analyzers'
              AND  subject_id = :analyzer_id
              AND  status::text IN ('QUEUED', 'RETRYING', 'RUNNING', 'BLOCKED')
            """
        ),
        {"analyzer_id": analyzer_id},
    )


async def experiment_ids_for_analyzer(
    session: AsyncSession, analyzer_id: str
) -> list[str]:
    stmt = select(analyzer_experiments.c.experiment_id).where(
        analyzer_experiments.c.analyzer_id == analyzer_id,
        analyzer_experiments.c.deleted_at.is_(None),
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

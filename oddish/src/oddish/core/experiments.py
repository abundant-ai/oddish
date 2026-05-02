from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db import ExperimentCellModel, ExperimentModel
from oddish.schemas import ExperimentListItemResponse


async def list_experiments_core(
    session: AsyncSession,
    *,
    org_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[ExperimentListItemResponse]:
    cell_counts = (
        select(
            ExperimentCellModel.experiment_id.label("experiment_id"),
            func.count(ExperimentCellModel.id).label("cell_count"),
        )
        .group_by(ExperimentCellModel.experiment_id)
        .subquery()
    )
    query = (
        select(
            ExperimentModel.id,
            ExperimentModel.name,
            ExperimentModel.is_public,
            ExperimentModel.public_token,
            ExperimentModel.created_at,
            func.coalesce(cell_counts.c.cell_count, 0),
        )
        .outerjoin(cell_counts, cell_counts.c.experiment_id == ExperimentModel.id)
        .order_by(ExperimentModel.created_at.desc(), ExperimentModel.id.desc())
        .limit(max(1, min(limit, 200)))
        .offset(max(0, offset))
    )
    if org_id is not None:
        query = query.where(ExperimentModel.org_id == org_id)

    rows = (await session.execute(query)).all()
    return [
        ExperimentListItemResponse(
            id=str(experiment_id),
            name=str(name),
            is_public=bool(is_public),
            public_token=public_token,
            cell_count=int(cell_count or 0),
            created_at=created_at,
        )
        for experiment_id, name, is_public, public_token, created_at, cell_count in rows
    ]

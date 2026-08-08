"""Admin API for excluding experiments from cost accounting."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth import AuthContext, can_manage_api_keys, require_admin
from auth.permissions import require_operator_org
from oddish.db import (
    CostExcludedExperimentModel,
    ExperimentModel,
    get_session,
    utcnow,
)

router = APIRouter(prefix="/admin/cost-excluded-experiments", tags=["Admin"])


class CostExcludedExperimentResponse(BaseModel):
    id: str
    experiment_id: str
    experiment_name: str
    label: str
    created_by: str | None
    created_at: str


class CreateCostExcludedExperimentRequest(BaseModel):
    experiment: str
    label: str = ""


def _response(row: CostExcludedExperimentModel) -> CostExcludedExperimentResponse:
    return CostExcludedExperimentResponse(
        id=row.id,
        experiment_id=row.experiment_id,
        experiment_name=row.experiment_name,
        label=row.label,
        created_by=row.created_by_user_id,
        created_at=row.created_at.isoformat(),
    )


def _require_manage(auth: AuthContext) -> None:
    require_operator_org(auth)
    if not can_manage_api_keys(auth):
        raise HTTPException(
            status_code=403,
            detail="Only organization admins may edit the cost-exclusion list",
        )


async def _resolve_experiment(session: AsyncSession, ref: str) -> ExperimentModel:
    # An exact id wins (include_deleted: spend from a soft-deleted experiment
    # still shows on cost surfaces, so it must stay excludable). Names resolve
    # among live experiments only and must be unambiguous -- experiment names
    # are not unique.
    by_id = await session.execute(
        select(ExperimentModel)
        .where(ExperimentModel.id == ref)
        .execution_options(include_deleted=True)
    )
    experiment = by_id.scalar_one_or_none()
    if experiment is not None:
        return experiment
    by_name = await session.execute(
        select(ExperimentModel).where(ExperimentModel.name == ref).limit(2)
    )
    matches = by_name.scalars().all()
    if len(matches) > 1:
        raise HTTPException(
            status_code=409,
            detail="experiment name is ambiguous; use the experiment id",
        )
    if not matches:
        raise HTTPException(status_code=404, detail="experiment not found")
    return matches[0]


@router.get("", response_model=list[CostExcludedExperimentResponse])
async def list_cost_excluded_experiments(
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> list[CostExcludedExperimentResponse]:
    require_operator_org(auth)
    async with get_session() as session:
        result = await session.execute(
            select(CostExcludedExperimentModel).order_by(
                CostExcludedExperimentModel.created_at.desc()
            )
        )
        return [_response(row) for row in result.scalars().all()]


@router.post("", response_model=CostExcludedExperimentResponse)
async def add_cost_excluded_experiment(
    request: CreateCostExcludedExperimentRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> CostExcludedExperimentResponse:
    _require_manage(auth)

    ref = request.experiment.strip()
    if not ref:
        raise HTTPException(status_code=400, detail="experiment must not be empty")

    async with get_session() as session:
        experiment = await _resolve_experiment(session, ref)
        existing = await session.execute(
            select(CostExcludedExperimentModel).where(
                CostExcludedExperimentModel.experiment_id == experiment.id
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(status_code=409, detail="experiment is already excluded")

        row = CostExcludedExperimentModel(
            experiment_id=experiment.id,
            experiment_name=experiment.name,
            label=request.label.strip(),
            created_by_user_id=auth.user_id,
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError:
            raise HTTPException(status_code=409, detail="experiment is already excluded")
        return _response(row)


@router.delete("/{row_id}")
async def remove_cost_excluded_experiment(
    row_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    _require_manage(auth)
    async with get_session() as session:
        result = await session.execute(
            select(CostExcludedExperimentModel).where(
                CostExcludedExperimentModel.id == row_id
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise HTTPException(
                status_code=404, detail="cost-excluded experiment not found"
            )
        row.deleted_at = utcnow()
        await session.commit()
    return {"deleted": row_id}

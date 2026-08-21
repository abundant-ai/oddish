from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from api.routers.cost_exclusions_shared import soft_delete, unavailable
from auth import AuthContext, require_admin
from auth.permissions import require_operator_org
from oddish.db import CostExcludedExperimentModel, ExperimentModel, get_session

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


async def _resolve_experiment(session: AsyncSession, ref: str) -> ExperimentModel:
    by_id = await session.scalars(
        select(ExperimentModel)
        .where(ExperimentModel.id == ref)
        .execution_options(include_deleted=True)
    )
    experiment = by_id.first()
    if experiment is not None:
        return experiment
    by_name = await session.scalars(
        select(ExperimentModel).where(ExperimentModel.name == ref).limit(2)
    )
    matches = by_name.all()
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
    try:
        async with get_session() as session:
            rows = await session.scalars(
                select(CostExcludedExperimentModel).order_by(
                    CostExcludedExperimentModel.created_at.desc()
                )
            )
            return [_response(row) for row in rows]
    except ProgrammingError as exc:
        raise unavailable(exc)


@router.post("", response_model=CostExcludedExperimentResponse)
async def add_cost_excluded_experiment(
    request: CreateCostExcludedExperimentRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> CostExcludedExperimentResponse:
    require_operator_org(auth)

    ref = request.experiment.strip()
    if not ref:
        raise HTTPException(status_code=400, detail="experiment must not be empty")

    try:
        async with get_session() as session:
            experiment = await _resolve_experiment(session, ref)
            if getattr(experiment, "is_collection", False):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "that experiment is a collection: it homes no trials of "
                        "its own, so excluding it would exclude nothing. Exclude "
                        "the experiments that actually ran the work."
                    ),
                )
            existing = await session.scalars(
                select(CostExcludedExperimentModel).where(
                    CostExcludedExperimentModel.experiment_id == experiment.id
                )
            )
            if existing.first() is not None:
                raise HTTPException(
                    status_code=409, detail="experiment is already excluded"
                )

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
                raise HTTPException(
                    status_code=409, detail="experiment is already excluded"
                )
            return _response(row)
    except ProgrammingError as exc:
        raise unavailable(exc)


@router.delete("/{row_id}")
async def remove_cost_excluded_experiment(
    row_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    require_operator_org(auth)
    try:
        async with get_session() as session:
            await soft_delete(
                session,
                CostExcludedExperimentModel,
                row_id,
                "cost-excluded experiment not found",
            )
    except ProgrammingError as exc:
        raise unavailable(exc)
    return {"deleted": row_id}

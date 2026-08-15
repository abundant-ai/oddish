"""Admin API for excluding an experiment's spend from cost accounting.

Operator-only and deployment-wide, like the model list. Scoped to trials
**homed** in the experiment, so a collection that merely gathers other
experiments' trials cannot launder their cost.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from auth import AuthContext, can_manage_api_keys, require_admin
from auth.permissions import require_operator_org
from oddish.db import (
    CostExcludedExperimentModel,
    ExperimentModel,
    get_session,
    utcnow,
)
from pg_errors import is_undefined_table_error

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


def _unavailable(exc: ProgrammingError) -> HTTPException:
    if is_undefined_table_error(exc):
        return HTTPException(
            503,
            "Cost exclusions are not available yet (schema is still "
            "migrating). Try again shortly.",
        )
    raise exc


async def _resolve_experiment(session: AsyncSession, ref: str) -> ExperimentModel:
    """The experiment an operator meant, by id or by name.

    An exact id wins, and resolves with ``include_deleted``: spend from a
    soft-deleted experiment still shows on cost surfaces, so it must stay
    excludable. Names resolve among live experiments only and must be
    unambiguous -- experiment names are not unique.
    """
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
        raise _unavailable(exc)


@router.post("", response_model=CostExcludedExperimentResponse)
async def add_cost_excluded_experiment(
    request: CreateCostExcludedExperimentRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> CostExcludedExperimentResponse:
    _require_manage(auth)

    ref = request.experiment.strip()
    if not ref:
        raise HTTPException(status_code=400, detail="experiment must not be empty")

    try:
        async with get_session() as session:
            experiment = await _resolve_experiment(session, ref)
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
                # A display snapshot: the row outlives the experiment.
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
        raise _unavailable(exc)


@router.delete("/{row_id}")
async def remove_cost_excluded_experiment(
    row_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    _require_manage(auth)
    try:
        async with get_session() as session:
            result = await session.scalars(
                select(CostExcludedExperimentModel).where(
                    CostExcludedExperimentModel.id == row_id
                )
            )
            row = result.first()
            if row is None:
                raise HTTPException(
                    status_code=404, detail="cost-excluded experiment not found"
                )
            row.deleted_at = utcnow()
            await session.commit()
    except ProgrammingError as exc:
        raise _unavailable(exc)
    return {"deleted": row_id}

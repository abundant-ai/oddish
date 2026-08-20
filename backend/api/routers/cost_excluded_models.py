from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import distinct, select
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from api.routers.cost_exclusions_shared import soft_delete, unavailable
from auth import AuthContext, require_admin
from auth.permissions import require_operator_org
from oddish.config import model_family_key
from oddish.core.cost_exclusions import canonical_excluded_model
from oddish.db import CostExcludedModelModel, TrialModel, get_session

router = APIRouter(prefix="/admin/cost-excluded-models", tags=["Admin"])


class CostExcludedModelResponse(BaseModel):
    id: str
    model_name: str
    label: str
    created_by: str | None
    created_at: str


class CreateCostExcludedModelRequest(BaseModel):
    model_name: str
    label: str = ""


def _response(row: CostExcludedModelModel) -> CostExcludedModelResponse:
    return CostExcludedModelResponse(
        id=row.id,
        model_name=row.model_name,
        label=row.label,
        created_by=row.created_by_user_id,
        created_at=row.created_at.isoformat(),
    )


async def _resolve_models(session: AsyncSession, ref: str) -> list[str]:
    key = model_family_key(ref)
    rows = await session.scalars(
        select(distinct(TrialModel.model)).where(TrialModel.model.isnot(None))
    )
    return sorted({m for m in rows if m and (m == ref or model_family_key(m) == key)})


@router.get("", response_model=list[CostExcludedModelResponse])
async def list_cost_excluded_models(
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> list[CostExcludedModelResponse]:
    require_operator_org(auth)
    try:
        async with get_session() as session:
            rows = await session.scalars(
                select(CostExcludedModelModel).order_by(
                    CostExcludedModelModel.model_name
                )
            )
            return [_response(row) for row in rows]
    except ProgrammingError as exc:
        raise unavailable(exc)


@router.post("", response_model=list[CostExcludedModelResponse])
async def add_cost_excluded_model(
    request: CreateCostExcludedModelRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> list[CostExcludedModelResponse]:
    require_operator_org(auth)

    if not canonical_excluded_model(request.model_name):
        raise HTTPException(status_code=400, detail="model must not be empty")

    try:
        async with get_session() as session:
            names = await _resolve_models(session, request.model_name.strip())
            if not names:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        "no trials have run on that model, so excluding it "
                        "would have no effect; check the spelling against the "
                        "model shown on a trial"
                    ),
                )

            existing = await session.scalars(
                select(CostExcludedModelModel.model_name).where(
                    CostExcludedModelModel.model_name.in_(names)
                )
            )
            fresh = [name for name in names if name not in set(existing)]
            if not fresh:
                raise HTTPException(status_code=409, detail="model is already excluded")

            rows = [
                CostExcludedModelModel(
                    model_name=name,
                    label=request.label.strip(),
                    created_by_user_id=auth.user_id,
                )
                for name in fresh
            ]
            session.add_all(rows)
            try:
                await session.commit()
            except IntegrityError:
                raise HTTPException(status_code=409, detail="model is already excluded")
            return [_response(row) for row in rows]
    except ProgrammingError as exc:
        raise unavailable(exc)


@router.delete("/{row_id}")
async def remove_cost_excluded_model(
    row_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    require_operator_org(auth)
    try:
        async with get_session() as session:
            await soft_delete(
                session, CostExcludedModelModel, row_id, "cost-excluded model not found"
            )
    except ProgrammingError as exc:
        raise unavailable(exc)
    return {"deleted": row_id}

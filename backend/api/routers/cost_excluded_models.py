"""Admin API for excluding whole models from cost accounting.

Operator-only and deployment-wide: a model on this list stops counting on the
admin cost dashboards and against quotas everywhere, for every org, and
retroactively -- the reason its spend isn't real (sponsored capacity, a free
preview tier) is a property of the model, not of when it ran.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, ProgrammingError

from auth import AuthContext, can_manage_api_keys, require_admin
from auth.permissions import require_operator_org
from oddish.core.cost_exclusions import canonical_excluded_model
from oddish.db import CostExcludedModelModel, get_session, utcnow
from pg_errors import is_undefined_table_error

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
        raise _unavailable(exc)


@router.post("", response_model=CostExcludedModelResponse)
async def add_cost_excluded_model(
    request: CreateCostExcludedModelRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> CostExcludedModelResponse:
    _require_manage(auth)

    # Store the canonical spelling so the live UNIQUE index collapses case and
    # whitespace variants, and so the stored value matches ``trials.model``
    # (normalized by the same function on write).
    model_name = canonical_excluded_model(request.model_name)
    if not model_name:
        raise HTTPException(status_code=400, detail="model must not be empty")

    try:
        async with get_session() as session:
            existing = await session.scalars(
                select(CostExcludedModelModel).where(
                    CostExcludedModelModel.model_name == model_name
                )
            )
            if existing.first() is not None:
                raise HTTPException(status_code=409, detail="model is already excluded")

            row = CostExcludedModelModel(
                model_name=model_name,
                label=request.label.strip(),
                created_by_user_id=auth.user_id,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                raise HTTPException(
                    status_code=409, detail="model is already excluded"
                )
            return _response(row)
    except ProgrammingError as exc:
        raise _unavailable(exc)


@router.delete("/{row_id}")
async def remove_cost_excluded_model(
    row_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    _require_manage(auth)
    try:
        async with get_session() as session:
            result = await session.scalars(
                select(CostExcludedModelModel).where(
                    CostExcludedModelModel.id == row_id
                )
            )
            row = result.first()
            if row is None:
                raise HTTPException(
                    status_code=404, detail="cost-excluded model not found"
                )
            row.deleted_at = utcnow()
            await session.commit()
    except ProgrammingError as exc:
        raise _unavailable(exc)
    return {"deleted": row_id}

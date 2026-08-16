from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import distinct, or_, select
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from auth import AuthContext, require_admin
from auth.permissions import require_operator_org
from oddish.config import normalize_model_id
from oddish.core.cost_exclusions import canonical_excluded_model
from oddish.db import CostExcludedModelModel, TrialModel, get_session, utcnow
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


def _unavailable(exc: ProgrammingError) -> HTTPException:
    if not is_undefined_table_error(exc):
        raise exc
    return HTTPException(
        503,
        "Cost exclusions are not available yet (schema is still migrating). "
        "Try again shortly.",
    )


async def _resolve_models(session: AsyncSession, ref: str) -> list[str]:
    """Every spelling of ``ref`` that ``trials.model`` actually stores.

    Exclusion matches ``trials.model`` verbatim, but that value is written by
    the agent-aware ``normalize_trial_model``, which re-routes ids: an operator
    who types ``kimi-k2`` has trials stored under ``moonshot/kimi-k2``, and
    ``claude-sonnet-4-5`` lands as a Bedrock id. Storing what they typed would
    register a row that silently matches nothing. So resolve against the values
    on disk and store those, and return every match -- one model can legitimately
    have two stored spellings (bare ``grok-free-preview`` and the routed
    ``xai/grok-free-preview``), and excluding only one would leave half the
    spend counting.
    """
    canonical = canonical_excluded_model(ref)
    rows = await session.scalars(
        select(distinct(TrialModel.model)).where(
            TrialModel.model.isnot(None),
            or_(
                TrialModel.model == ref,
                TrialModel.model == canonical,
                TrialModel.model.ilike(f"%/{canonical}"),
            ),
        )
    )
    stored = [m for m in rows if m]
    # The ILIKE can over-match on a suffix (``a/b-c`` for ``b-c``); confirm in
    # Python against the same normalization the writer used.
    return sorted(
        {
            m
            for m in stored
            if m == ref
            or normalize_model_id(m) == canonical
            or (normalize_model_id(m) or "").endswith(f"/{canonical}")
        }
    )


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
        raise _unavailable(exc)


@router.delete("/{row_id}")
async def remove_cost_excluded_model(
    row_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    require_operator_org(auth)
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

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, ProgrammingError

from api.routers.cost_exclusions_shared import soft_delete, unavailable
from auth import AuthContext, require_admin
from auth.permissions import require_operator_org
from oddish.core.llm_key_fingerprint import hash_llm_key, key_hint
from oddish.db import CostExcludedLlmKeyModel, get_session

router = APIRouter(prefix="/admin/cost-excluded-keys", tags=["Admin"])


class CostExcludedKeyResponse(BaseModel):
    id: str
    key_hint: str
    label: str
    created_by: str | None
    created_at: str


class CreateCostExcludedKeyRequest(BaseModel):
    key: str
    label: str = ""


def _response(row: CostExcludedLlmKeyModel) -> CostExcludedKeyResponse:
    return CostExcludedKeyResponse(
        id=row.id,
        key_hint=row.key_hint,
        label=row.label,
        created_by=row.created_by_user_id,
        created_at=row.created_at.isoformat(),
    )


@router.get("", response_model=list[CostExcludedKeyResponse])
async def list_cost_excluded_keys(
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> list[CostExcludedKeyResponse]:
    require_operator_org(auth)
    try:
        async with get_session() as session:
            rows = await session.scalars(
                select(CostExcludedLlmKeyModel).order_by(
                    CostExcludedLlmKeyModel.created_at.desc()
                )
            )
            return [_response(row) for row in rows]
    except ProgrammingError as exc:
        raise unavailable(exc)


@router.post("", response_model=CostExcludedKeyResponse)
async def add_cost_excluded_key(
    request: CreateCostExcludedKeyRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> CostExcludedKeyResponse:
    require_operator_org(auth)
    key = request.key.strip()
    if len(key) < 8:
        raise HTTPException(status_code=400, detail="key is too short")

    row = CostExcludedLlmKeyModel(
        key_hash=hash_llm_key(key),
        key_hint=key_hint(key),
        label=request.label.strip(),
        created_by_user_id=auth.user_id,
    )
    try:
        async with get_session() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                raise HTTPException(status_code=409, detail="key is already excluded")
            return _response(row)
    except ProgrammingError as exc:
        raise unavailable(exc)


@router.delete("/{row_id}")
async def remove_cost_excluded_key(
    row_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    require_operator_org(auth)
    try:
        async with get_session() as session:
            await soft_delete(
                session,
                CostExcludedLlmKeyModel,
                row_id,
                "cost-excluded key not found",
            )
    except ProgrammingError as exc:
        raise unavailable(exc)
    return {"deleted": row_id}

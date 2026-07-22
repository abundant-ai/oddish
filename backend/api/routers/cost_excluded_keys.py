"""Admin API for the LLM-key cost-exclusion list.

Admins register LLM provider API keys (e.g. sponsored/free keys) whose spend is
ignored across quota enforcement and every cost surface. Only a one-way SHA-256
hash and a masked hint are stored -- the pasted key is hashed and discarded.
Trials stamped with a matching hash are dropped by ``first_party_spend_filter``.

Writes require a JWT org admin: a FULL-scope API key passes ``require_admin`` but
must not be able to edit what counts as spend, so the mutating routes re-check
``can_manage_api_keys`` (JWT-admin-only).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from auth import AuthContext, can_manage_api_keys, require_admin
from oddish.core.llm_key_fingerprint import hash_llm_key, key_hint
from oddish.db import CostExcludedLlmKeyModel, get_session, utcnow

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


def _require_manage(auth: AuthContext) -> None:
    if not can_manage_api_keys(auth):
        raise HTTPException(
            status_code=403,
            detail="Only organization admins may edit the cost-exclusion list",
        )


@router.get("", response_model=list[CostExcludedKeyResponse])
async def list_cost_excluded_keys(
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> list[CostExcludedKeyResponse]:
    async with get_session() as session:
        result = await session.execute(
            select(CostExcludedLlmKeyModel).order_by(
                CostExcludedLlmKeyModel.created_at.desc()
            )
        )
        return [_response(row) for row in result.scalars().all()]


@router.post("", response_model=CostExcludedKeyResponse)
async def add_cost_excluded_key(
    request: CreateCostExcludedKeyRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> CostExcludedKeyResponse:
    _require_manage(auth)

    key = request.key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="key must not be empty")
    key_hash = hash_llm_key(key)

    async with get_session() as session:
        existing = await session.execute(
            select(CostExcludedLlmKeyModel).where(
                CostExcludedLlmKeyModel.key_hash == key_hash
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(status_code=409, detail="key is already excluded")

        row = CostExcludedLlmKeyModel(
            key_hash=key_hash,
            key_hint=key_hint(key),
            label=request.label.strip(),
            created_by_user_id=auth.user_id,
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError:
            # Concurrent add of the same key: the partial unique index on live
            # key_hash rows is the arbiter, so surface the same 409 the
            # existence pre-check would have returned.
            raise HTTPException(status_code=409, detail="key is already excluded")
        return _response(row)


@router.delete("/{key_id}")
async def remove_cost_excluded_key(
    key_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    _require_manage(auth)
    async with get_session() as session:
        result = await session.execute(
            select(CostExcludedLlmKeyModel).where(
                CostExcludedLlmKeyModel.id == key_id
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="cost-excluded key not found")
        row.deleted_at = utcnow()
        await session.commit()
    return {"deleted": key_id}

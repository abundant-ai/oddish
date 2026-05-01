"""Probe-preset CRUD — replaces the flat-file probe-presets.json on the FE.

The endpoint surface mirrors the previous Next.js route (GET returns the
full list, PUT replaces it) so the frontend's existing fetch/persist code
keeps working. Presets are scoped by ``auth.org_id``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select

from auth import APIKeyScope, AuthContext, require_auth
from oddish.db import ProbePreset, get_session, utcnow

router = APIRouter(prefix="/probe-presets", tags=["Probe Presets"])


class PresetIn(BaseModel):
    id: str
    name: str
    agent: str
    model: str
    operator_prompt: str
    result_focus: str | None = None
    evaluation_metric: str | None = None
    ratio_unit: str | None = None
    ratio_verb: str | None = None
    include_prior_attempts: dict | None = None
    is_seed: bool = False


def _to_dict(p: ProbePreset) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "agent": p.agent,
        "model": p.model,
        "operator_prompt": p.operator_prompt,
        "result_focus": p.result_focus,
        "evaluation_metric": p.evaluation_metric,
        "ratio_unit": p.ratio_unit,
        "ratio_verb": p.ratio_verb,
        "include_prior_attempts": p.include_prior_attempts,
        "is_seed": p.is_seed,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


@router.get("")
async def list_presets(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> list[dict[str, Any]]:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        result = await session.execute(
            select(ProbePreset).where(ProbePreset.org_id == auth.org_id)
        )
        return [_to_dict(p) for p in result.scalars().all()]


@router.put("")
async def replace_presets(
    presets: list[PresetIn],
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict[str, Any]:
    """Replace the org's full preset list. Matches the old PUT /api/probe-presets shape."""
    auth.require_scope(APIKeyScope.TASKS)
    if auth.org_id is None:
        raise HTTPException(status_code=400, detail="org_id required")

    now = utcnow()
    async with get_session() as session:
        await session.execute(
            delete(ProbePreset).where(ProbePreset.org_id == auth.org_id)
        )
        for p in presets:
            session.add(
                ProbePreset(
                    id=p.id,
                    org_id=auth.org_id,
                    name=p.name,
                    agent=p.agent,
                    model=p.model,
                    operator_prompt=p.operator_prompt,
                    result_focus=p.result_focus,
                    evaluation_metric=p.evaluation_metric,
                    ratio_unit=p.ratio_unit,
                    ratio_verb=p.ratio_verb,
                    include_prior_attempts=p.include_prior_attempts,
                    is_seed=p.is_seed,
                    created_at=now,
                    updated_at=now,
                )
            )
        await session.commit()
    return {"ok": True, "count": len(presets)}

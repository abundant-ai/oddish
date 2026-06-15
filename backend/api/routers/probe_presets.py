"""CRUD endpoints for probe presets.

Thin wrapper over ``oddish.core.probe_presets``: each handler authenticates,
opens a session, delegates to the core function, commits, and serializes the
result. Seeds are read-only (the core layer raises 403 on mutation).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from auth import APIKeyScope, AuthContext, require_auth
from oddish.core.probe_presets import (
    create_probe_preset_core,
    delete_probe_preset_core,
    list_probe_presets_core,
    update_probe_preset_core,
)
from oddish.db import get_session
from oddish.schemas import (
    ProbePresetCreate,
    ProbePresetResponse,
    ProbePresetUpdate,
)

router = APIRouter()


@router.get("/probe-presets", response_model=list[ProbePresetResponse])
async def list_probe_presets(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> list[ProbePresetResponse]:
    """List global seed presets plus the authenticated org's custom presets."""
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        presets = await list_probe_presets_core(session, org_id=auth.org_id)
        return [ProbePresetResponse.model_validate(p) for p in presets]


@router.post("/probe-presets", response_model=ProbePresetResponse)
async def create_probe_preset(
    data: ProbePresetCreate,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> ProbePresetResponse:
    """Create a custom preset for the authenticated org."""
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        preset = await create_probe_preset_core(session, data=data, org_id=auth.org_id)
        await session.commit()
        return ProbePresetResponse.model_validate(preset)


@router.put("/probe-presets/{preset_id}", response_model=ProbePresetResponse)
async def update_probe_preset(
    preset_id: str,
    data: ProbePresetUpdate,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> ProbePresetResponse:
    """Update a custom preset owned by the authenticated org (seeds 403)."""
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        preset = await update_probe_preset_core(
            session, preset_id=preset_id, data=data, org_id=auth.org_id
        )
        await session.commit()
        return ProbePresetResponse.model_validate(preset)


@router.delete("/probe-presets/{preset_id}")
async def delete_probe_preset(
    preset_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict[str, bool]:
    """Soft-delete a custom preset owned by the authenticated org (seeds 403)."""
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        await delete_probe_preset_core(session, preset_id=preset_id, org_id=auth.org_id)
        await session.commit()
        return {"ok": True}

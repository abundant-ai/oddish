"""Core CRUD for probe presets.

Follows the repo's router->core layering: these functions receive an
``AsyncSession`` and never open their own session or commit — the calling
router owns the transaction. ``org_id`` is passed explicitly.

Seed presets (``is_seed=True``, ``org_id`` NULL) are global, read-only
built-ins; custom presets are org-scoped and mutable by their owning org.
The session-level soft-delete listener auto-applies ``deleted_at IS NULL``
to these ORM selects, so callers never see tombstoned rows.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.result_focus_schema import (
    UnsupportedSchemaError,
    normalize_findings_schema,
    parse_result_focus,
)
from oddish.db import ProbePresetModel, utcnow
from oddish.schemas import ProbePresetCreate, ProbePresetUpdate


async def list_probe_presets_core(
    session: AsyncSession,
    *,
    org_id: str | None = None,
) -> list[ProbePresetModel]:
    """Return global seed presets plus the org's custom presets.

    Ordered seeds-first, then by name, so the built-ins surface at the top
    of the picker.
    """
    query = (
        select(ProbePresetModel)
        .where(
            or_(
                ProbePresetModel.org_id == org_id,
                ProbePresetModel.org_id.is_(None),
            )
        )
        .order_by(ProbePresetModel.is_seed.desc(), ProbePresetModel.name)
    )
    result = await session.execute(query)
    return list(result.scalars().all())


async def create_probe_preset_core(
    session: AsyncSession,
    *,
    data: ProbePresetCreate,
    org_id: str | None = None,
) -> ProbePresetModel:
    """Create a custom (non-seed) preset owned by ``org_id``."""
    spec = parse_result_focus(data.result_focus)
    if spec is not None:
        try:
            normalize_findings_schema(spec)
        except UnsupportedSchemaError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    preset = ProbePresetModel(
        org_id=org_id,
        name=data.name,
        agent=data.agent,
        model=data.model,
        operator_prompt=data.operator_prompt,
        result_focus=data.result_focus,
        evaluation_metric=data.evaluation_metric,
        is_seed=False,
    )
    session.add(preset)
    await session.flush()
    return preset


async def _get_owned_preset(
    session: AsyncSession,
    *,
    preset_id: str,
    org_id: str | None,
) -> ProbePresetModel:
    """Fetch a preset and authorize it for mutation by ``org_id``.

    Raises 403 for seed presets (read-only) and 404 when the preset is
    missing or belongs to a different org.
    """
    result = await session.execute(
        select(ProbePresetModel).where(ProbePresetModel.id == preset_id)
    )
    preset = result.scalar_one_or_none()
    if preset is None:
        raise HTTPException(status_code=404, detail=f"Preset {preset_id} not found")
    if preset.is_seed:
        raise HTTPException(status_code=403, detail="Seed presets are read-only")
    if preset.org_id != org_id:
        raise HTTPException(status_code=404, detail=f"Preset {preset_id} not found")
    return preset


async def update_probe_preset_core(
    session: AsyncSession,
    *,
    preset_id: str,
    data: ProbePresetUpdate,
    org_id: str | None = None,
) -> ProbePresetModel:
    """Apply provided fields to an org-owned custom preset."""
    preset = await _get_owned_preset(session, preset_id=preset_id, org_id=org_id)
    updates = data.model_dump(exclude_unset=True)
    if "result_focus" in updates:
        spec = parse_result_focus(updates["result_focus"])
        if spec is not None:
            try:
                normalize_findings_schema(spec)
            except UnsupportedSchemaError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
    for field, value in updates.items():
        setattr(preset, field, value)
    await session.flush()
    return preset


async def delete_probe_preset_core(
    session: AsyncSession,
    *,
    preset_id: str,
    org_id: str | None = None,
) -> None:
    """Soft-delete an org-owned custom preset."""
    preset = await _get_owned_preset(session, preset_id=preset_id, org_id=org_id)
    preset.deleted_at = utcnow()
    await session.flush()

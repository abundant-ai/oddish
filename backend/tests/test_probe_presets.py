"""Core CRUD tests for probe presets.

Exercises ``oddish.core.probe_presets`` against the real local Postgres
(same pattern as ``test_local_runner.py``: a session-scoped org id, seed +
teardown via id/org-scoped deletes). Run with your backend env (the one
providing ``ODDISH_DATABASE_URL``, e.g. ``backend/.env``) sourced and the
``probe_presets_001`` migration applied:

    set -a && source .env && set +a && uv run pytest tests/test_probe_presets.py
"""

import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException

from oddish.core.probe_presets import (
    create_probe_preset_core,
    delete_probe_preset_core,
    list_probe_presets_core,
    update_probe_preset_core,
)
from oddish.db import ProbePresetModel, get_session
from oddish.schemas import ProbePresetCreate, ProbePresetUpdate


@pytest_asyncio.fixture
async def org_id():
    """A unique org id; teardown hard-deletes any presets it created
    (including soft-deleted ones, hence include_deleted)."""
    oid = f"org_pp_{uuid.uuid4().hex[:8]}"
    yield oid
    async with get_session() as session:
        await session.execute(
            ProbePresetModel.__table__.delete().where(ProbePresetModel.org_id == oid)
        )


def _make(name: str) -> ProbePresetCreate:
    return ProbePresetCreate(
        name=name,
        agent="claude-code",
        model="anthropic/claude-sonnet-4-6",
        operator_prompt="probe the task",
        result_focus="did it work?",
        evaluation_metric="cheat_ratio",
    )


@pytest.mark.asyncio
async def test_create_then_list_includes_custom_and_seeds(org_id):
    async with get_session() as session:
        created = await create_probe_preset_core(
            session, data=_make("My probe"), org_id=org_id
        )
        await session.commit()
        created_id = created.id

    async with get_session() as session:
        presets = await list_probe_presets_core(session, org_id=org_id)

    ids = {p.id for p in presets}
    assert created_id in ids
    # Seeds (global) come through too, and sort first.
    assert "cheat-detector" in ids
    assert presets[0].is_seed is True
    custom = next(p for p in presets if p.id == created_id)
    assert custom.is_seed is False
    assert custom.org_id == org_id


@pytest.mark.asyncio
async def test_list_is_org_scoped(org_id):
    async with get_session() as session:
        created = await create_probe_preset_core(
            session, data=_make("Org A probe"), org_id=org_id
        )
        await session.commit()
        created_id = created.id

    other_org = f"org_pp_other_{uuid.uuid4().hex[:8]}"
    async with get_session() as session:
        other = await list_probe_presets_core(session, org_id=other_org)
    # The custom preset must not leak into a different org's list.
    assert created_id not in {p.id for p in other}


@pytest.mark.asyncio
async def test_update_changes_fields(org_id):
    async with get_session() as session:
        created = await create_probe_preset_core(
            session, data=_make("Editable"), org_id=org_id
        )
        await session.commit()
        created_id = created.id

    async with get_session() as session:
        updated = await update_probe_preset_core(
            session,
            preset_id=created_id,
            data=ProbePresetUpdate(name="Renamed", result_focus="new focus"),
            org_id=org_id,
        )
        await session.commit()
        assert updated.name == "Renamed"
        assert updated.result_focus == "new focus"
        # Untouched fields preserved.
        assert updated.operator_prompt == "probe the task"


@pytest.mark.asyncio
async def test_update_seed_is_rejected(org_id):
    async with get_session() as session:
        with pytest.raises(HTTPException) as exc:
            await update_probe_preset_core(
                session,
                preset_id="cheat-detector",
                data=ProbePresetUpdate(name="hijack"),
                org_id=org_id,
            )
        assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_delete_seed_is_rejected(org_id):
    async with get_session() as session:
        with pytest.raises(HTTPException) as exc:
            await delete_probe_preset_core(
                session, preset_id="cheat-detector", org_id=org_id
            )
        assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_delete_soft_removes_from_list(org_id):
    async with get_session() as session:
        created = await create_probe_preset_core(
            session, data=_make("Disposable"), org_id=org_id
        )
        await session.commit()
        created_id = created.id

    async with get_session() as session:
        await delete_probe_preset_core(session, preset_id=created_id, org_id=org_id)
        await session.commit()

    async with get_session() as session:
        presets = await list_probe_presets_core(session, org_id=org_id)
    assert created_id not in {p.id for p in presets}


@pytest.mark.asyncio
async def test_update_missing_is_404(org_id):
    async with get_session() as session:
        with pytest.raises(HTTPException) as exc:
            await update_probe_preset_core(
                session,
                preset_id="does-not-exist",
                data=ProbePresetUpdate(name="x"),
                org_id=org_id,
            )
        assert exc.value.status_code == 404

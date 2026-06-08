"""Core CRUD tests for skills. Run with backend env sourced and skills tables present:

    set -a && source .env && set +a && uv run pytest tests/test_skills.py
"""

import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException

from oddish.core.skills import (
    create_skill_core,
    delete_skill_core,
    get_skill_core,
    list_skills_core,
    update_skill_core,
)
from oddish.db import SkillModel, get_session
from oddish.schemas import SkillCreate, SkillFile, SkillUpdate


def _payload(name="my-skill"):
    md = f"---\nname: {name}\ndescription: useful\n---\nbody"
    return SkillCreate(
        name=name,
        description="useful",
        files=[
            SkillFile(relative_path="SKILL.md", content=md),
            SkillFile(relative_path="scripts/run.sh", content="echo hi"),
        ],
    )


@pytest_asyncio.fixture
async def org_id():
    oid = f"org_sk_{uuid.uuid4().hex[:8]}"
    yield oid
    async with get_session() as session:
        await session.execute(
            SkillModel.__table__.delete().where(SkillModel.org_id == oid)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_create_and_get(org_id):
    async with get_session() as session:
        created = await create_skill_core(
            session, data=_payload(), org_id=org_id, user_id="user_1"
        )
        await session.commit()
        skill_id = created.id
    async with get_session() as session:
        got = await get_skill_core(session, skill_id, org_id=org_id)
        assert got.name == "my-skill"
        assert got.created_by_user_id == "user_1"
        assert {f.relative_path for f in got.files} == {"SKILL.md", "scripts/run.sh"}


@pytest.mark.asyncio
async def test_list_returns_org_skills(org_id):
    async with get_session() as session:
        await create_skill_core(session, data=_payload("a"), org_id=org_id, user_id="u")
        await create_skill_core(session, data=_payload("b"), org_id=org_id, user_id="u")
        await session.commit()
    async with get_session() as session:
        skills = await list_skills_core(session, org_id=org_id)
        names = {s.name for s in skills}
        assert {"a", "b"} <= names


@pytest.mark.asyncio
async def test_other_org_cannot_see(org_id):
    async with get_session() as session:
        await create_skill_core(session, data=_payload("secret"), org_id=org_id, user_id="u")
        await session.commit()
    async with get_session() as session:
        skills = await list_skills_core(session, org_id="org_other")
        assert "secret" not in {s.name for s in skills}


@pytest.mark.asyncio
async def test_update_replaces_files(org_id):
    async with get_session() as session:
        created = await create_skill_core(session, data=_payload(), org_id=org_id, user_id="u")
        await session.commit()
        skill_id = created.id
    new_md = "---\nname: my-skill\ndescription: changed\n---\nbody"
    upd = SkillUpdate(
        description="changed",
        files=[SkillFile(relative_path="SKILL.md", content=new_md)],
    )
    async with get_session() as session:
        await update_skill_core(session, skill_id, data=upd, org_id=org_id)
        await session.commit()
    async with get_session() as session:
        got = await get_skill_core(session, skill_id, org_id=org_id)
        assert got.description == "changed"
        assert {f.relative_path for f in got.files} == {"SKILL.md"}


@pytest.mark.asyncio
async def test_delete_soft_deletes(org_id):
    async with get_session() as session:
        created = await create_skill_core(session, data=_payload(), org_id=org_id, user_id="u")
        await session.commit()
        skill_id = created.id
    async with get_session() as session:
        await delete_skill_core(session, skill_id, org_id=org_id)
        await session.commit()
    async with get_session() as session:
        with pytest.raises(HTTPException) as exc:
            await get_skill_core(session, skill_id, org_id=org_id)
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_cannot_mutate_other_org(org_id):
    async with get_session() as session:
        created = await create_skill_core(session, data=_payload(), org_id=org_id, user_id="u")
        await session.commit()
        skill_id = created.id
    async with get_session() as session:
        with pytest.raises(HTTPException) as exc:
            await delete_skill_core(session, skill_id, org_id="org_other")
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_other_org_returns_404(org_id):
    async with get_session() as session:
        created = await create_skill_core(session, data=_payload(), org_id=org_id, user_id="u")
        await session.commit()
        skill_id = created.id
    async with get_session() as session:
        with pytest.raises(HTTPException) as exc:
            await get_skill_core(session, skill_id, org_id="org_other")
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_update_other_org_returns_404(org_id):
    async with get_session() as session:
        created = await create_skill_core(session, data=_payload(), org_id=org_id, user_id="u")
        await session.commit()
        skill_id = created.id
    async with get_session() as session:
        with pytest.raises(HTTPException) as exc:
            await update_skill_core(
                session, skill_id, data=SkillUpdate(description="x"), org_id="org_other"
            )
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_update_without_files_patches_fields(org_id):
    async with get_session() as session:
        created = await create_skill_core(session, data=_payload(), org_id=org_id, user_id="u")
        await session.commit()
        skill_id = created.id
    async with get_session() as session:
        await update_skill_core(
            session, skill_id, data=SkillUpdate(description="patched"), org_id=org_id
        )
        await session.commit()
    async with get_session() as session:
        got = await get_skill_core(session, skill_id, org_id=org_id)
        assert got.description == "patched"
        assert {f.relative_path for f in got.files} == {"SKILL.md", "scripts/run.sh"}


@pytest.mark.asyncio
async def test_seed_skill_is_read_only():
    seed_name = f"seed-{uuid.uuid4().hex[:8]}"
    async with get_session() as session:
        seed = SkillModel(
            org_id=None,
            created_by_user_id=None,
            name=seed_name,
            description="builtin",
            is_seed=True,
        )
        session.add(seed)
        await session.commit()
        seed_id = seed.id
    try:
        async with get_session() as session:
            with pytest.raises(HTTPException) as exc:
                await delete_skill_core(session, seed_id, org_id="any_org")
            assert exc.value.status_code == 403
        async with get_session() as session:
            with pytest.raises(HTTPException) as exc:
                await update_skill_core(
                    session, seed_id, data=SkillUpdate(description="hack"), org_id="any_org"
                )
            assert exc.value.status_code == 403
    finally:
        async with get_session() as session:
            # hard-delete the seed row (bypass soft-delete) so it doesn't leak between runs
            await session.execute(
                SkillModel.__table__.delete().where(SkillModel.id == seed_id)
            )
            await session.commit()

import pytest
from fastapi import HTTPException

from oddish.core.skills import create_skill_core, update_skill_core
from oddish.schemas import SkillCreate, SkillFile, SkillUpdate

SKILL_MD = "---\nname: probe-skill\ndescription: a probe\n---\nbody text"


def _files():
    return [SkillFile(relative_path="SKILL.md", content=SKILL_MD)]


@pytest.mark.asyncio
async def test_create_skill_persists_directive_fields(session):
    skill = await create_skill_core(
        session,
        data=SkillCreate(
            name="probe-skill",
            description="a probe",
            files=_files(),
            operator_prompt="probe the verifier",
            result_focus="what bug?",
            evaluation_metric="result_focus",
        ),
        org_id="org1",
    )
    assert skill.operator_prompt == "probe the verifier"
    assert skill.result_focus == "what bug?"
    assert skill.evaluation_metric == "result_focus"


@pytest.mark.asyncio
async def test_create_skill_rejects_bad_result_focus_schema(session):
    with pytest.raises(HTTPException) as exc:
        await create_skill_core(
            session,
            data=SkillCreate(
                name="probe-skill",
                description="a probe",
                files=_files(),
                result_focus='{"type": "nonsense-type"}',
            ),
            org_id="org1",
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_update_skill_sets_directive_fields(session):
    created = await create_skill_core(
        session,
        data=SkillCreate(name="probe-skill", description="a probe", files=_files()),
        org_id="org1",
    )
    updated = await update_skill_core(
        session,
        created.id,
        data=SkillUpdate(operator_prompt="new directive"),
        org_id="org1",
    )
    assert updated.operator_prompt == "new directive"

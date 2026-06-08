"""Tests for staging an org's skills into a probe task dir (phase-2 injection).

Run with backend env sourced and the skills tables present:

    set -a && source .env && set +a && uv run pytest tests/test_probe_staging_skills.py
"""

import uuid

import pytest
import pytest_asyncio

from oddish.core.skills import create_skill_core
from oddish.db import SkillModel, get_session
from oddish.schemas import SkillCreate, SkillFile
from oddish.worker.probe_staging import stage_org_skills


def _payload(name):
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
    oid = f"org_stage_{uuid.uuid4().hex[:8]}"
    yield oid
    async with get_session() as session:
        await session.execute(
            SkillModel.__table__.delete().where(SkillModel.org_id == oid)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_stages_org_skills_into_claude_dir(org_id, tmp_path):
    async with get_session() as session:
        await create_skill_core(session, data=_payload("alpha"), org_id=org_id, user_id="u")
        await create_skill_core(session, data=_payload("beta"), org_id=org_id, user_id="u")
        await session.commit()

    n = await stage_org_skills(tmp_path, org_id=org_id)
    assert n == 2

    skills_root = tmp_path / ".claude" / "skills"
    assert (skills_root / "alpha" / "SKILL.md").read_text().startswith("---")
    assert (skills_root / "alpha" / "scripts" / "run.sh").read_text() == "echo hi"
    assert (skills_root / "beta" / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_no_skills_stages_nothing(org_id, tmp_path):
    # org with no skills -> nothing written, no error
    n = await stage_org_skills(tmp_path, org_id=org_id)
    assert n == 0
    assert not (tmp_path / ".claude").exists()


@pytest.mark.asyncio
async def test_one_bad_skill_does_not_block_others(org_id, tmp_path, monkeypatch):
    async with get_session() as session:
        await create_skill_core(session, data=_payload("good"), org_id=org_id, user_id="u")
        await session.commit()

    # Force materialize_skills to raise for one specific bundle name, proving
    # per-skill resilience: the failure is swallowed and counted as not-staged,
    # but the call itself never raises.
    import oddish.worker.probe_staging as ps

    real = ps.materialize_skills

    def flaky(bundles, root):
        if bundles and bundles[0].name == "good":
            raise RuntimeError("boom")
        return real(bundles, root)

    monkeypatch.setattr(ps, "materialize_skills", flaky)
    n = await stage_org_skills(tmp_path, org_id=org_id)
    assert n == 0  # the only skill failed, but no exception propagated


@pytest.mark.asyncio
async def test_apply_probe_overlay_routes_org_id_to_skills(org_id, tmp_path):
    """The full overlay (what both runners call) stages the org's skills.

    Uses a bare temp task dir: related-log and harbor-source staging are
    best-effort and degrade silently, so no real task/trial row is needed —
    this isolates that ``org_id`` flows through to ``stage_org_skills``.
    """
    from oddish.worker.probe_staging import apply_probe_overlay

    async with get_session() as session:
        await create_skill_core(session, data=_payload("gamma"), org_id=org_id, user_id="u")
        await session.commit()

    (tmp_path / "instruction.md").write_text("original task spec")
    await apply_probe_overlay(
        tmp_path,
        task_id="no-such-task",
        trial_id="no-such-trial",
        extra_instructions="follow the operator directive",
        org_id=org_id,
    )

    assert (tmp_path / ".claude" / "skills" / "gamma" / "SKILL.md").exists()
    # instruction.md was still rewritten with the directive (overlay ran fully)
    assert "follow the operator directive" in (tmp_path / "instruction.md").read_text()

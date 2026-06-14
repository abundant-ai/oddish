"""Tests for staging an org's skills into a Harbor skills-root.

``stage_org_skills`` materializes the org's skills (+ seeds) into a root whose
children are per-skill dirs (each with SKILL.md) — the exact layout Harbor's
``AgentConfig.skills`` / ``resolve_skills`` expects. The runners hand that root
to Harbor, which uploads the skills into the sandbox.

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
async def test_stages_skills_into_root_per_skill_dirs(org_id, tmp_path):
    async with get_session() as session:
        await create_skill_core(session, data=_payload("alpha"), org_id=org_id, user_id="u")
        await create_skill_core(session, data=_payload("beta"), org_id=org_id, user_id="u")
        await session.commit()

    skills_root = tmp_path / "agent_skills"
    n = await stage_org_skills(skills_root, org_id=org_id)
    assert n == 2

    # Layout is <root>/<name>/<relative_path> — NOT nested under .claude/skills.
    assert (skills_root / "alpha" / "SKILL.md").read_text().startswith("---")
    assert (skills_root / "alpha" / "scripts" / "run.sh").read_text() == "echo hi"
    assert (skills_root / "beta" / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_staged_root_is_harbor_resolvable(org_id, tmp_path):
    """The produced root must satisfy Harbor's resolve_skills contract:
    a root whose every child dir contains SKILL.md resolves to one skill each."""
    from harbor.skills import resolve_skills

    async with get_session() as session:
        await create_skill_core(session, data=_payload("alpha"), org_id=org_id, user_id="u")
        await create_skill_core(session, data=_payload("beta"), org_id=org_id, user_id="u")
        await session.commit()

    skills_root = tmp_path / "agent_skills"
    await stage_org_skills(skills_root, org_id=org_id)

    resolved = resolve_skills([skills_root])
    assert {s.name for s in resolved} == {"alpha", "beta"}


@pytest.mark.asyncio
async def test_no_skills_stages_nothing(org_id, tmp_path):
    skills_root = tmp_path / "agent_skills"
    n = await stage_org_skills(skills_root, org_id=org_id)
    assert n == 0
    # Nothing materialized (root may not even be created).
    assert not skills_root.exists() or not any(skills_root.iterdir())


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
    n = await stage_org_skills(tmp_path / "agent_skills", org_id=org_id)
    assert n == 0  # the only skill failed, but no exception propagated


@pytest.mark.asyncio
async def test_apply_probe_overlay_does_not_stage_skills(org_id, tmp_path):
    """Overlay must NOT write skills into the task dir (Harbor never mounts it).
    Skill delivery is the runners' job via AgentConfig.skills."""
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
    )

    # No skills written into the task dir, but the directive still applied.
    assert not (tmp_path / ".claude").exists()
    assert "follow the operator directive" in (tmp_path / "instruction.md").read_text()


@pytest.mark.asyncio
async def test_apply_probe_overlay_writes_brief_and_visibility_map(tmp_path):
    """The original spec is saved to AGENT_BRIEF.md and the rewritten
    instruction.md carries a visibility map that splits environment/ (real
    agent's view) from probe-only paths (tests/, solution/, ...)."""
    from oddish.worker.probe_staging import apply_probe_overlay

    (tmp_path / "instruction.md").write_text("the real agent's brief")
    env = tmp_path / "environment"
    env.mkdir()
    (env / "Dockerfile").write_text("FROM python")
    (env / "src").mkdir()
    (env / "src" / "app.py").write_text("print('hi')")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_hidden.py").write_text("assert True")
    (tmp_path / "solution").mkdir()

    await apply_probe_overlay(
        tmp_path,
        task_id="no-such-task",
        trial_id="no-such-trial",
        extra_instructions="be adversarial",
    )

    # The agent's brief is preserved verbatim as a standalone reference file.
    assert (tmp_path / "AGENT_BRIEF.md").read_text() == "the real agent's brief"

    instr = (tmp_path / "instruction.md").read_text()
    assert "WHAT THE REAL AGENT SEES" in instr
    # environment/ files are the real agent's view ...
    assert "environment/Dockerfile" in instr
    assert "environment/src/app.py" in instr
    # ... while tests/ + solution/ are flagged probe-only.
    assert "tests/" in instr
    assert "solution/" in instr
    # The brief itself and instruction.md are never listed as probe-only.
    assert "AGENT_BRIEF.md" not in instr.split("PROBE-ONLY", 1)[1]

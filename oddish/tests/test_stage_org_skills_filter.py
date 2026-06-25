import pytest

from oddish.worker import probe_staging


class _Skill:
    def __init__(self, id, name):
        self.id = id
        self.name = name
        self.files = []  # no SKILL.md -> materialize is a no-op stub below


@pytest.mark.asyncio
async def test_stage_filters_to_selected_ids(tmp_path, monkeypatch):
    skills = [_Skill("a", "alpha"), _Skill("b", "beta"), _Skill("c", "gamma")]

    async def fake_list(session, *, org_id=None):
        return skills

    materialized = []

    def fake_materialize(bundles, root):
        materialized.extend(b.name for b in bundles)

    # list_skills_core is imported into probe_staging's namespace.
    monkeypatch.setattr(probe_staging, "list_skills_core", fake_list)
    monkeypatch.setattr(probe_staging, "materialize_skills", fake_materialize)

    class _Sess:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(probe_staging, "get_session", lambda: _Sess())

    n = await probe_staging.stage_org_skills(
        tmp_path, org_id="org1", skill_ids=["b"]
    )
    assert n == 1
    assert materialized == ["beta"]


@pytest.mark.asyncio
async def test_stage_empty_ids_mounts_nothing(tmp_path, monkeypatch):
    async def fake_list(session, *, org_id=None):
        return [_Skill("a", "alpha")]
    monkeypatch.setattr(probe_staging, "list_skills_core", fake_list)
    monkeypatch.setattr(probe_staging, "materialize_skills", lambda b, r: None)

    class _Sess:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(probe_staging, "get_session", lambda: _Sess())

    n = await probe_staging.stage_org_skills(tmp_path, org_id="org1", skill_ids=[])
    assert n == 0

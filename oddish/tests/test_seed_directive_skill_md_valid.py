from oddish.alembic_seed_directives import SEED_DIRECTIVE_SKILLS  # see step 3
from oddish.core.skills import parse_skill
from oddish.schemas import SkillFile


def test_each_seed_directive_has_valid_skill_md():
    for s in SEED_DIRECTIVE_SKILLS:
        files = [SkillFile(relative_path="SKILL.md", content=s["skill_md"])]
        name, description = parse_skill(files)  # raises if invalid
        assert name and description
        assert s["operator_prompt"]


def test_cheat_detector_seed_present():
    ids = {s["id"] for s in SEED_DIRECTIVE_SKILLS}
    assert "cheat-detector" in ids

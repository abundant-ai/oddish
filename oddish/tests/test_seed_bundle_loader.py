from oddish.core.skills import parse_skill
from oddish.schemas import SkillFile
from oddish.seeds.loader import load_seed_bundles


def test_loads_nine_bundles_each_with_valid_skill_md():
    bundles = load_seed_bundles()
    names = {b["name"] for b in bundles}
    assert len(bundles) == 9
    assert "task-review-agent-guide" in names
    for b in bundles:
        files = [SkillFile(relative_path=p, content=c) for p, c in b["files"]]
        parse_skill(files)  # raises if SKILL.md missing/invalid


def test_bundles_have_no_operator_prompt():
    for b in load_seed_bundles():
        assert "operator_prompt" not in b or b.get("operator_prompt") is None

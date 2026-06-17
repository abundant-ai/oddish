"""Pure tests for skill materialization (phase-2 injection seam)."""

from pathlib import Path

import pytest

from oddish.worker.skills_overlay import SkillBundle, materialize_skills


def test_writes_nested_tree(tmp_path: Path):
    bundles = [
        SkillBundle(
            name="my-skill",
            files=[
                ("SKILL.md", "---\nname: my-skill\ndescription: d\n---\n"),
                ("scripts/run.sh", "echo hi"),
            ],
        )
    ]
    written = materialize_skills(bundles, tmp_path)
    assert (tmp_path / "my-skill" / "SKILL.md").read_text().startswith("---")
    assert (tmp_path / "my-skill" / "scripts" / "run.sh").read_text() == "echo hi"
    expected = {
        (tmp_path / "my-skill" / "SKILL.md").resolve(),
        (tmp_path / "my-skill" / "scripts" / "run.sh").resolve(),
    }
    assert {p.resolve() for p in written} == expected


def test_multiple_skills_isolated(tmp_path: Path):
    bundles = [
        SkillBundle(name="a", files=[("SKILL.md", "a")]),
        SkillBundle(name="b", files=[("SKILL.md", "b")]),
    ]
    materialize_skills(bundles, tmp_path)
    assert (tmp_path / "a" / "SKILL.md").read_text() == "a"
    assert (tmp_path / "b" / "SKILL.md").read_text() == "b"


def test_rejects_path_escape(tmp_path: Path):
    bundles = [SkillBundle(name="x", files=[("../escape.sh", "evil")])]
    with pytest.raises(ValueError):
        materialize_skills(bundles, tmp_path)

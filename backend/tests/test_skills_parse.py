"""Pure unit tests for skill frontmatter validation."""

import pytest
from fastapi import HTTPException

from oddish.core.skills import parse_skill
from oddish.schemas import SkillFile

VALID_SKILL_MD = """---
name: my-skill
description: Does a useful thing
---

# My Skill

Body here.
"""


def test_parses_name_and_description():
    files = [SkillFile(relative_path="SKILL.md", content=VALID_SKILL_MD)]
    name, description = parse_skill(files)
    assert name == "my-skill"
    assert description == "Does a useful thing"


def test_missing_skill_md_raises_422():
    files = [SkillFile(relative_path="scripts/run.sh", content="echo hi")]
    with pytest.raises(HTTPException) as exc:
        parse_skill(files)
    assert exc.value.status_code == 422


def test_missing_name_raises_422():
    md = "---\ndescription: no name here\n---\nbody"
    files = [SkillFile(relative_path="SKILL.md", content=md)]
    with pytest.raises(HTTPException) as exc:
        parse_skill(files)
    assert exc.value.status_code == 422


def test_missing_description_raises_422():
    md = "---\nname: only-name\n---\nbody"
    files = [SkillFile(relative_path="SKILL.md", content=md)]
    with pytest.raises(HTTPException) as exc:
        parse_skill(files)
    assert exc.value.status_code == 422


def test_malformed_frontmatter_raises_422():
    md = "---\nname: [unclosed\n---\nbody"
    files = [SkillFile(relative_path="SKILL.md", content=md)]
    with pytest.raises(HTTPException) as exc:
        parse_skill(files)
    assert exc.value.status_code == 422


def test_no_frontmatter_raises_422():
    files = [SkillFile(relative_path="SKILL.md", content="# Just a heading")]
    with pytest.raises(HTTPException) as exc:
        parse_skill(files)
    assert exc.value.status_code == 422

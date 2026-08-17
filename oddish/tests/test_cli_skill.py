"""The packaged oddish SKILL.md and the ``oddish skill`` command."""

from __future__ import annotations

import typer
from typer.testing import CliRunner

from oddish.cli.skill import SKILL_DIR_NAME, skill, skill_text


def _invoke(args: list[str]):
    app = typer.Typer()
    app.command()(skill)
    return CliRunner().invoke(app, args)


def test_packaged_skill_has_valid_frontmatter():
    text = skill_text()
    assert text.startswith("---\n")
    frontmatter = text.split("---\n", 2)[1]
    assert "name: oddish" in frontmatter
    assert "description:" in frontmatter


def test_skill_prints_markdown_to_stdout():
    result = _invoke([])
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("---\n")
    assert "oddish backfill-analysis" in result.stdout
    assert "\x1b[" not in result.stdout


def test_skill_install_writes_skill_md_under_named_dir(tmp_path):
    result = _invoke(["--install", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    dest = tmp_path / SKILL_DIR_NAME / "SKILL.md"
    assert dest.read_text() == skill_text()


def test_skill_install_detects_existing_skills_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".claude" / "skills").mkdir(parents=True)
    result = _invoke(["--install"])
    assert result.exit_code == 0, result.output
    dest = tmp_path / ".claude" / "skills" / SKILL_DIR_NAME / "SKILL.md"
    assert dest.read_text() == skill_text()


def test_skill_path_prints_packaged_location():
    result = _invoke(["--path"])
    assert result.exit_code == 0, result.output
    assert "SKILL.md" in result.stdout

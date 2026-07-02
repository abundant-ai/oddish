from pathlib import Path

import httpx
from typer.testing import CliRunner

from oddish.cli import app
from oddish.cli.probe import _collect_skill_files


def _set_env(monkeypatch):
    monkeypatch.setenv("ODDISH_API_KEY", "ok_test")
    monkeypatch.setenv("ODDISH_API_URL", "https://api.example.test")


def _write_skill(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: does a thing\n---\n\n# My Skill\n"
    )
    (root / "scripts").mkdir()
    (root / "scripts" / "run.sh").write_text("echo hi")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "junk.pyc").write_text("nope")
    (root / ".DS_Store").write_text("nope")


def test_collect_skill_files_filters_junk_and_uses_posix(tmp_path):
    _write_skill(tmp_path / "skill")
    files = _collect_skill_files(tmp_path / "skill")
    paths = sorted(f["relative_path"] for f in files)
    assert paths == ["SKILL.md", "scripts/run.sh"]


def test_skill_add_missing_skill_md_errors(tmp_path, monkeypatch):
    _set_env(monkeypatch)
    (tmp_path / "empty").mkdir()
    result = CliRunner().invoke(app, ["probe", "skill", "add", str(tmp_path / "empty")])
    assert result.exit_code == 1
    assert "SKILL.md" in result.output


class _FakeClient:
    last_request: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, json=None):
        _FakeClient.last_request = {"url": url, "json": json}
        return httpx.Response(200, json={"id": "skill_abc", "name": "my-skill-2"})


def test_skill_add_posts_and_reports_stored_name(tmp_path, monkeypatch):
    _set_env(monkeypatch)
    _write_skill(tmp_path / "skill")
    _FakeClient.last_request = {}
    monkeypatch.setattr(httpx, "Client", _FakeClient)

    result = CliRunner().invoke(app, ["probe", "skill", "add", str(tmp_path / "skill")])

    assert result.exit_code == 0, result.output
    req = _FakeClient.last_request
    assert req["url"] == "https://api.example.test/skills"
    assert req["json"]["name"] == "my-skill"
    assert req["json"]["description"] == "does a thing"
    assert sorted(f["relative_path"] for f in req["json"]["files"]) == [
        "SKILL.md",
        "scripts/run.sh",
    ]
    assert "my-skill-2" in result.output  # server's stored (versioned) name
    assert "skill_abc" in result.output

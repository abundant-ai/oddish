from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.cli import app

runner = CliRunner()

# A build-context fetch: the only ERROR this otherwise-clean task trips.
# make_task's default task.toml is network_mode="no-network" (closed_internet
# passes) and the .dockerignore suppresses the .git WARN, so exactly one
# finding fires — a provenance fetch.
_FETCH_DOCKERFILE = "FROM ubuntu:24.04\nRUN git clone https://github.com/foo/bar /src\n"


def test_preflight_exits_zero_on_a_clean_task(make_task):
    task_dir = make_task(extra_files={"environment/.dockerignore": "**/.git\n"})
    result = runner.invoke(app, ["preflight", str(task_dir)])
    assert result.exit_code == 0


def test_preflight_exits_one_on_an_error_finding(make_task):
    task_dir = make_task(
        dockerfile=_FETCH_DOCKERFILE, extra_files={"environment/.dockerignore": "**/.git\n"}
    )
    result = runner.invoke(app, ["preflight", str(task_dir)])
    assert result.exit_code == 1
    assert "provenance" in result.output


def test_preflight_exits_zero_when_only_warnings(make_task):
    # No .dockerignore -> a WARN from provenance, nothing more.
    task_dir = make_task()
    result = runner.invoke(app, ["preflight", str(task_dir)])
    assert result.exit_code == 0


def test_preflight_json_emits_a_parseable_document(make_task):
    task_dir = make_task(
        dockerfile=_FETCH_DOCKERFILE, extra_files={"environment/.dockerignore": "**/.git\n"}
    )
    result = runner.invoke(app, ["preflight", str(task_dir), "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["findings"][0]["check_id"] == "provenance"
    assert payload["findings"][0]["severity"] == "error"


def test_preflight_json_ok_true_on_clean_task(make_task):
    task_dir = make_task(extra_files={"environment/.dockerignore": "**/.git\n"})
    result = runner.invoke(app, ["preflight", str(task_dir), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["findings"] == []


run_module = importlib.import_module("oddish.cli.run")


def _stub_run_preamble(monkeypatch):
    """Neutralize the API-key/URL preamble that runs before the preflight gate."""
    monkeypatch.setattr(run_module, "get_api_url", lambda *a, **k: "http://x")
    monkeypatch.setattr(run_module, "require_api_key", lambda *a, **k: "key")


def test_run_aborts_before_upload_when_preflight_fails(make_task, monkeypatch):
    task_dir = make_task(
        dockerfile=_FETCH_DOCKERFILE, extra_files={"environment/.dockerignore": "**/.git\n"}
    )
    _stub_run_preamble(monkeypatch)

    uploaded: list[object] = []
    monkeypatch.setattr(
        run_module,
        "upload_tasks_with_progress",
        lambda *a, **k: uploaded.append(a) or [],
    )

    result = runner.invoke(app, ["run", str(task_dir), "--agent", "claude-code"])
    assert result.exit_code == 1
    assert uploaded == [], "preflight must abort before any upload happens"


def test_run_force_proceeds_and_still_prints_findings(make_task, monkeypatch):
    task_dir = make_task(
        dockerfile=_FETCH_DOCKERFILE, extra_files={"environment/.dockerignore": "**/.git\n"}
    )
    _stub_run_preamble(monkeypatch)

    def _sentinel(*a, **k):
        raise RuntimeError("reached upload")

    monkeypatch.setattr(run_module, "upload_tasks_with_progress", _sentinel)

    result = runner.invoke(
        app, ["run", str(task_dir), "--agent", "claude-code", "--force"]
    )
    # Getting past the gate is proved by the sentinel firing.
    assert "reached upload" in str(result.exception)
    assert "provenance" in result.output

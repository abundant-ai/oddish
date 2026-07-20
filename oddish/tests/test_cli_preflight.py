from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest
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

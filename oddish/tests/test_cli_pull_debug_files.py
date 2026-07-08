"""Tests for ``oddish pull --debug-files``.

Fakes the HTTP client to assert the CLI calls the trial debug-files endpoint and
renders / emits the S3 inventory without downloading anything.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import typer  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

_DEBUG_PAYLOAD = {
    "trial_id": "task-abc-0",
    "trial_s3_key": None,
    "computed_prefix": "tasks/task-abc/trials/task-abc-0/",
    "using_prefix": "tasks/task-abc/trials/task-abc-0/",
    "harbor_result_path": None,
    "files": ["tasks/task-abc/trials/task-abc-0/result.json"],
    "trajectory_files": [],
    "error": None,
}


def _make_client(status_code: int, payload: dict, calls: list[str]):
    class _Resp:
        def __init__(self):
            self.status_code = status_code

        def json(self):
            return payload

        @property
        def text(self):
            return json.dumps(payload)

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a, **k):
            return False

        def get(self, url, params=None):
            calls.append(url)
            return _Resp()

    return _Client


def _invoke(status_code: int, payload: dict, extra: list[str]):
    import importlib

    pull_module = importlib.import_module("oddish.cli.pull")
    calls: list[str] = []
    fake = _make_client(status_code, payload, calls)

    app = typer.Typer()
    app.command()(pull_module.pull)
    runner = CliRunner()
    with patch("oddish.cli.pull.httpx.Client", fake):
        with patch("oddish.cli.pull.require_api_key"):
            with patch("oddish.cli.pull.get_api_url", return_value="http://localhost"):
                with patch("oddish.cli.pull.get_auth_headers", return_value={}):
                    result = runner.invoke(
                        app, ["task-abc-0", "--debug-files", "--type", "trial", *extra]
                    )
    return result, calls


def test_debug_files_lists_inventory():
    result, calls = _invoke(200, _DEBUG_PAYLOAD, [])
    assert result.exit_code == 0, result.output
    assert any(c.endswith("/trials/task-abc-0/debug-files") for c in calls)
    assert "tasks/task-abc/trials/task-abc-0/result.json" in result.output


def test_debug_files_json_output():
    result, _ = _invoke(200, _DEBUG_PAYLOAD, ["--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert doc["trial_id"] == "task-abc-0"
    assert doc["files"] == ["tasks/task-abc/trials/task-abc-0/result.json"]


def test_debug_files_rejects_non_trial_type():
    import importlib

    pull_module = importlib.import_module("oddish.cli.pull")
    app = typer.Typer()
    app.command()(pull_module.pull)
    runner = CliRunner()
    with patch("oddish.cli.pull.require_api_key"):
        with patch("oddish.cli.pull.get_api_url", return_value="http://localhost"):
            result = runner.invoke(
                app, ["some-task", "--debug-files", "--type", "task"]
            )
    assert result.exit_code != 0

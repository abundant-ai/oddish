"""Tests for the ``oddish costs`` command."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import typer  # noqa: E402
from typer.testing import CliRunner  # noqa: E402


def _invoke(status_code: int, payload: object, args: list[str]):
    import importlib

    costs_module = importlib.import_module("oddish.cli.costs")
    captured: dict = {}

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
            captured["url"] = url
            captured["params"] = dict(params or {})
            return _Resp()

    app = typer.Typer()
    app.command()(costs_module.costs)
    runner = CliRunner()
    with patch("oddish.cli.costs.httpx.Client", _Client):
        with patch("oddish.cli.costs.require_api_key"):
            with patch("oddish.cli.costs.get_api_url", return_value="http://localhost"):
                with patch("oddish.cli.costs.get_auth_headers", return_value={}):
                    result = runner.invoke(app, args)
    captured["result"] = result
    return captured


_ORG = {
    "totals": {"cost_usd": 12.5, "trial_count": 40, "user_count": 3, "experiment_count": 5},
    "by_user": [{"key": "u1", "name": "Alice", "email": "a@x.com", "cost_usd": 8.0, "trial_count": 25}],
    "by_model": [{"model": "claude-sonnet-4-5", "provider": "anthropic", "cost_usd": 10.0, "trial_count": 30}],
    "experiments": [],
}
_USER = {
    "billed_user_id": "u1",
    "name": "Alice",
    "email": "a@x.com",
    "totals": {"cost_usd": 8.0, "trial_count": 25, "task_count": 4},
    "tasks": [{"task_id": "t1", "task_name": "demo", "cost_usd": 5.0, "trial_count": 10, "models": []}],
}


def test_costs_org_breakdown():
    cap = _invoke(200, _ORG, [])
    assert cap["result"].exit_code == 0, cap["result"].output
    assert cap["url"].endswith("/admin/costs")
    assert cap["params"]["window_days"] == 7
    assert "Total spend" in cap["result"].output
    assert "Alice" in cap["result"].output


def test_costs_user_breakdown_with_window():
    cap = _invoke(200, _USER, ["--user", "u1", "--window-days", "30"])
    assert cap["result"].exit_code == 0, cap["result"].output
    assert cap["url"].endswith("/admin/costs/users/u1")
    assert cap["params"]["window_days"] == 30
    assert "Billed spend" in cap["result"].output


def test_costs_auth_error_exits_nonzero():
    cap = _invoke(403, {"detail": "Admin role required"}, [])
    assert cap["result"].exit_code == 1
    assert "full-scope API key" in cap["result"].output


def test_costs_core_server_404_hint():
    cap = _invoke(404, {"detail": "Not Found"}, [])
    assert cap["result"].exit_code == 1
    assert "hosted Oddish" in cap["result"].output


def test_costs_json_output():
    cap = _invoke(200, _ORG, ["--json"])
    assert cap["result"].exit_code == 0, cap["result"].output
    assert json.loads(cap["result"].output)["totals"]["cost_usd"] == 12.5

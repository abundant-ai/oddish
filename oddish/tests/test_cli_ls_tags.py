"""Tests for the new tag flags on ``oddish ls``."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from typer.testing import CliRunner  # noqa: E402


def _runner():
    import importlib

    ls_module = importlib.import_module("oddish.cli.ls")

    runner = CliRunner()
    return runner, ls_module


def test_ls_passes_tag_params_as_csv():
    runner, ls_module = _runner()

    captured = {}

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {"items": [], "has_more": False, "limit": 25, "offset": 0}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a, **k):
            return False

        def get(self, url, params):
            captured["params"] = dict(params)
            return _FakeResponse()

    import typer

    app = typer.Typer()
    app.command()(ls_module.ls)

    with patch("oddish.cli.ls.httpx.Client", _FakeClient):
        with patch("oddish.cli.ls.require_api_key"):
            with patch("oddish.cli.ls.get_api_url", return_value="http://localhost"):
                with patch("oddish.cli.ls.get_auth_headers", return_value={}):
                    result = runner.invoke(
                        app,
                        [
                            "--tag",
                            "flaky",
                            "--tag",
                            "swe-bench",
                            "--tag-any",
                            "regression",
                            "--not-tag",
                            "wip",
                            "--json",
                        ],
                    )
    assert result.exit_code == 0
    assert captured["params"]["tags"] == "flaky,swe-bench"
    assert captured["params"]["tags_any"] == "regression"
    assert captured["params"]["tags_none"] == "wip"

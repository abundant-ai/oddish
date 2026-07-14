from __future__ import annotations

from pathlib import Path
import sys

import httpx
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.cli import app


class _FakeClient:
    last_request: dict = {}

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None):
        _FakeClient.last_request = {"url": url, "json": json}
        return httpx.Response(200, json={"id": "r1", "name": "Q3", "status": "pending"})


def _set_env(monkeypatch):
    monkeypatch.setenv("ODDISH_API_KEY", "ok_test")
    monkeypatch.setenv("ODDISH_API_URL", "https://api.example.test")


def test_report_create_posts_expected_payload(monkeypatch):
    _FakeClient.last_request = {}
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    _set_env(monkeypatch)

    result = CliRunner().invoke(
        app, ["report", "create", "-e", "e1", "-e", "e2", "--name", "Q3"]
    )
    assert result.exit_code == 0, result.output
    assert _FakeClient.last_request["url"] == "https://api.example.test/reports"
    assert _FakeClient.last_request["json"] == {"experiment_ids": ["e1", "e2"], "name": "Q3"}
    assert "r1" in result.output


def test_report_create_omits_name_when_unset(monkeypatch):
    _FakeClient.last_request = {}
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    _set_env(monkeypatch)

    result = CliRunner().invoke(app, ["report", "create", "-e", "e1"])
    assert result.exit_code == 0, result.output
    # No name key -> server auto-generates report_<N>_<exp>.
    assert _FakeClient.last_request["json"] == {"experiment_ids": ["e1"]}


def test_report_create_requires_experiments(monkeypatch):
    class _Exploding(_FakeClient):
        def post(self, url, json=None):
            raise AssertionError("HTTP should not be called with no experiments")

    monkeypatch.setattr(httpx, "Client", _Exploding)
    _set_env(monkeypatch)
    result = CliRunner().invoke(app, ["report", "create", "--name", "Q3"])
    assert result.exit_code == 1

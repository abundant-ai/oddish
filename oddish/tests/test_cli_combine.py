from __future__ import annotations

from pathlib import Path
import sys

import httpx
import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.cli import app
from oddish.cli.combine import _format_combine_summary, _normalize_sources


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_normalize_sources_strips_blanks_and_dedupes():
    assert _normalize_sources(["a", " a ", "b", "", "  ", "b"]) == ["a", "b"]


def test_normalize_sources_preserves_order():
    assert _normalize_sources(["c", "a", "b", "a"]) == ["c", "a", "b"]


def test_format_summary_includes_counts_and_omits_skipped_when_zero():
    lines = _format_combine_summary(
        {
            "id": "exp9",
            "name": "combo",
            "source_experiment_ids": ["a", "b"],
            "tasks_linked": 3,
            "trials_copied": 10,
            "trials_skipped": 0,
            "artifacts_copied": 40,
        }
    )
    blob = "\n".join(lines)
    assert "exp9" in blob and "combo" in blob
    assert "Sources combined: 2" in blob
    assert "Tasks linked:     3" in blob
    assert "Trials copied:    10" in blob
    assert "Artifacts copied: 40" in blob
    assert "skipped" not in blob.lower()


def test_format_summary_shows_skipped_when_nonzero():
    lines = _format_combine_summary({"id": "e", "name": "n", "trials_skipped": 4})
    assert any("Trials skipped:   4" in line for line in lines)


# ---------------------------------------------------------------------------
# Command (httpx mocked)
# ---------------------------------------------------------------------------


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
        return httpx.Response(
            200,
            json={
                "id": "new123",
                "name": "combo",
                "source_experiment_ids": ["a", "b"],
                "tasks_linked": 3,
                "trials_copied": 10,
                "trials_skipped": 0,
                "artifacts_copied": 40,
            },
        )


class _ExplodingClient:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, *args, **kwargs):
        raise AssertionError("HTTP must not be called for an invalid request")


def _set_env(monkeypatch):
    monkeypatch.setenv("ODDISH_API_KEY", "ok_test")
    monkeypatch.setenv("ODDISH_API_URL", "https://api.example.test")


def test_combine_posts_expected_payload(monkeypatch):
    _FakeClient.last_request = {}
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    _set_env(monkeypatch)

    result = CliRunner().invoke(
        app,
        ["combine", "a", " a ", "b", "--name", "combo", "--no-copy-artifacts"],
    )

    assert result.exit_code == 0, result.output
    assert (
        _FakeClient.last_request["url"]
        == "https://api.example.test/experiments/combine"
    )
    assert _FakeClient.last_request["json"] == {
        "source_experiment_ids": ["a", "b"],
        "copy_artifacts": False,
        "name": "combo",
    }
    assert "new123" in result.output


def test_combine_defaults_copy_artifacts_true_and_omits_name(monkeypatch):
    _FakeClient.last_request = {}
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    _set_env(monkeypatch)

    result = CliRunner().invoke(app, ["combine", "a", "b"])

    assert result.exit_code == 0, result.output
    assert _FakeClient.last_request["json"] == {
        "source_experiment_ids": ["a", "b"],
        "copy_artifacts": True,
    }


def test_combine_rejects_single_distinct_source_without_http(monkeypatch):
    # Collapses to one source -> rejected client-side, before any HTTP call.
    monkeypatch.setattr(httpx, "Client", _ExplodingClient)
    _set_env(monkeypatch)

    result = CliRunner().invoke(app, ["combine", "a", " a "])

    assert result.exit_code == 1
    assert "at least two" in result.output.lower()


@pytest.mark.parametrize("status,body", [(404, "nope"), (403, "forbidden")])
def test_combine_reports_api_error(monkeypatch, status, body):
    class _ErrClient(_FakeClient):
        def post(self, url, json=None):
            return httpx.Response(status, text=body)

    monkeypatch.setattr(httpx, "Client", _ErrClient)
    _set_env(monkeypatch)

    result = CliRunner().invoke(app, ["combine", "a", "b"])

    assert result.exit_code == 1
    assert str(status) in result.output

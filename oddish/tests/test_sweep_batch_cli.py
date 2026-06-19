"""Batch sweep CLI fallback safety: never double-submit on ambiguous failures.

``submit_sweep_batch`` may fall back to per-task submission ONLY when it knows
the batch route did nothing -- HTTP 404/405 on an older server. Any ambiguous
failure (read timeout, connection error, 5xx, unexpected body) could land after
the server already committed the batch; since idempotent replay is deferred, the
function surfaces the error instead of returning ``None`` so the run command does
not resubmit every task and create duplicate trials.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
import typer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import oddish.cli.api as api  # noqa: E402
from oddish.cli.api import submit_sweep_batch  # noqa: E402


class _Resp:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json


class _Client:
    """Single-shot fake httpx.Client: returns a response or raises an exception."""

    def __init__(self, outcome):
        self._outcome = outcome
        self.calls: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, **kwargs):
        self.calls.append(url)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def _install(monkeypatch, outcome) -> _Client:
    client = _Client(outcome)
    monkeypatch.setattr(api.httpx, "Client", lambda *_a, **_k: client)
    monkeypatch.setattr(api, "get_auth_headers", lambda: {})
    return client


PAYLOADS = [{"task_id": "t-a", "configs": []}, {"task_id": "t-b", "configs": []}]


@pytest.mark.parametrize("status", [404, 405])
def test_missing_batch_route_falls_back(monkeypatch, status):
    """404/405 == route absent, nothing created -> None so the caller falls back."""
    client = _install(monkeypatch, _Resp(status))
    assert submit_sweep_batch("http://api", PAYLOADS) is None
    assert client.calls == ["http://api/tasks/sweep/batch"]  # attempted once


def test_read_timeout_does_not_fall_back(monkeypatch):
    """A read timeout may land after commit -> surface, never return None."""
    client = _install(monkeypatch, httpx.ReadTimeout("timed out"))
    with pytest.raises(typer.Exit):
        submit_sweep_batch("http://api", PAYLOADS)
    # The request was attempted exactly once and did NOT fall back per task.
    assert client.calls == ["http://api/tasks/sweep/batch"]


def test_server_error_does_not_fall_back(monkeypatch):
    """A 5xx may have committed some/all items -> surface, never return None."""
    _install(monkeypatch, _Resp(500, text="overloaded"))
    with pytest.raises(typer.Exit):
        submit_sweep_batch("http://api", PAYLOADS)


def test_unexpected_body_does_not_fall_back(monkeypatch):
    """200 with no results array still means processed -> do not fall back."""
    _install(monkeypatch, _Resp(200, json_data={"unexpected": True}))
    with pytest.raises(typer.Exit):
        submit_sweep_batch("http://api", PAYLOADS)


@pytest.mark.parametrize("status", [200, 207])
def test_processed_batch_returns_results(monkeypatch, status):
    """200 (all ok) and 207 (mixed) both return the per-item results array."""
    results = [
        {"index": 0, "success": True, "status_code": 200, "task": {"id": "t-a"}},
        {"index": 1, "success": False, "status_code": 404, "error": "missing"},
    ]
    _install(monkeypatch, _Resp(status, json_data={"results": results}))
    assert submit_sweep_batch("http://api", PAYLOADS) == results

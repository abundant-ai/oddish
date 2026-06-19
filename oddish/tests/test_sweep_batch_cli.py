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
from oddish.cli.run import _map_batch_sweep_results  # noqa: E402


class _Resp:
    def __init__(self, status_code, json_data=None, text="", json_exc=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text
        self._json_exc = json_exc

    def json(self):
        if self._json_exc is not None:
            raise self._json_exc
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


def test_invalid_json_body_does_not_fall_back(monkeypatch):
    """A 200 whose body is not valid JSON is a clean exit, not a raw crash."""
    _install(monkeypatch, _Resp(200, json_exc=ValueError("no json here")))
    with pytest.raises(typer.Exit):
        submit_sweep_batch("http://api", PAYLOADS)


def test_non_object_body_does_not_fall_back(monkeypatch):
    """A 207 whose JSON body is not an object (e.g. a list) is a clean exit."""
    _install(monkeypatch, _Resp(207, json_data=[1, 2, 3]))
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


# ---------------------------------------------------------------------------
# _map_batch_sweep_results (run.py): per-item mapping must not crash on junk.
# ---------------------------------------------------------------------------

_TARGETS = [("t-a", True, None, None), ("t-b", True, None, None)]


def test_map_results_malformed_item_exits():
    """A non-dict result item is a clean exit, not an AttributeError crash."""
    with pytest.raises(typer.Exit):
        _map_batch_sweep_results(["not-a-dict"], _TARGETS)


def test_map_results_maps_success_and_failure():
    """Well-formed items map to ordered task dicts, trial totals, and failures."""
    results = [
        {"index": 0, "success": True, "task": {"id": "t-a", "trials_count": 2}},
        {"index": 1, "success": False, "error": "missing"},
    ]
    by_index, total, failed = _map_batch_sweep_results(results, _TARGETS)
    assert by_index == [{"id": "t-a", "trials_count": 2}, None]
    assert total == 2
    assert failed == ["t-b"]


# ---------------------------------------------------------------------------
# Chunking: a single unbounded batch is rejected (HTTP 303, nothing committed)
# once it exceeds the server's per-request ceiling, so submit_sweep_batch splits
# the payload into capped chunks and re-bases each item's index globally.
# ---------------------------------------------------------------------------


class _QueueClient:
    """Fake httpx.Client returning a queued outcome per post() call."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.posted: list[list[dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, **kwargs):
        self.posted.append(kwargs["json"]["submissions"])
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _install_queue(monkeypatch, outcomes) -> _QueueClient:
    client = _QueueClient(outcomes)
    monkeypatch.setattr(api.httpx, "Client", lambda *_a, **_k: client)
    monkeypatch.setattr(api, "get_auth_headers", lambda: {})
    return client


def _ok_chunk(n):
    """A 200 response carrying chunk-local indices 0..n-1."""
    return _Resp(
        200,
        json_data={
            "results": [
                {"index": i, "success": True, "task": {"id": f"local-{i}"}}
                for i in range(n)
            ]
        },
    )


def test_large_batch_is_chunked_and_indices_rebased(monkeypatch):
    """>cap payloads split into per-chunk requests; indices re-based globally."""
    monkeypatch.setenv("ODDISH_SWEEP_BATCH_MAX_TASKS", "2")
    client = _install_queue(monkeypatch, [_ok_chunk(2), _ok_chunk(2), _ok_chunk(1)])
    payloads = [{"task_id": f"t-{i}", "configs": []} for i in range(5)]

    out = submit_sweep_batch("http://api", payloads)

    # Five payloads, cap 2 -> three requests of 2 + 2 + 1.
    assert [len(s) for s in client.posted] == [2, 2, 1]
    # Each chunk reports local indices; the aggregate is re-based onto 0..4.
    assert [r["index"] for r in out] == [0, 1, 2, 3, 4]


def test_first_chunk_missing_route_falls_back(monkeypatch):
    """404 on the FIRST chunk -> None (nothing committed) so caller falls back."""
    monkeypatch.setenv("ODDISH_SWEEP_BATCH_MAX_TASKS", "2")
    client = _install_queue(monkeypatch, [_Resp(404)])
    payloads = [{"task_id": f"t-{i}", "configs": []} for i in range(5)]

    assert submit_sweep_batch("http://api", payloads) is None
    assert len(client.posted) == 1  # stopped after the first chunk


def test_later_chunk_failure_does_not_fall_back(monkeypatch):
    """A later chunk failing after earlier chunks committed must surface, not None."""
    monkeypatch.setenv("ODDISH_SWEEP_BATCH_MAX_TASKS", "2")
    _install_queue(monkeypatch, [_ok_chunk(2), _Resp(500, text="boom")])
    payloads = [{"task_id": f"t-{i}", "configs": []} for i in range(4)]

    with pytest.raises(typer.Exit):
        submit_sweep_batch("http://api", payloads)

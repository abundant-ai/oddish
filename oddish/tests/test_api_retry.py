"""Jittered, budgeted retry for the idempotent task-upload calls.

``_retry_request`` wraps ``/tasks/upload/init`` and ``/tasks/upload/complete``
with capped exponential backoff + full jitter, a token-bucket retry budget,
and ``Retry-After`` support. It retries only transient statuses
(429/500/502/503/504) and transport errors -- never other 4xx. ``/tasks/sweep``
is deliberately NOT wrapped.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
import typer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import oddish.cli.api as api  # noqa: E402
from oddish.cli.api import (  # noqa: E402
    _RetryBudget,
    _retry_request,
    submit_sweep,
)


class _RecordingRng:
    """rng.uniform stub: records (low, high) and returns the high bound."""

    def __init__(self):
        self.calls: list[tuple[float, float]] = []

    def uniform(self, low, high):
        self.calls.append((low, high))
        return high


def _resp(status: int, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status_code=status, headers=headers or {})


def _scripted_send(responses):
    """Return a zero-arg send() that yields the given responses/exceptions."""
    seq = list(responses)

    def send():
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    send.remaining = lambda: len(seq)
    return send


# ---------------------------------------------------------------------------
# _retry_request
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retries_transient_then_succeeds(status):
    sleeps: list[float] = []
    send = _scripted_send([_resp(status), _resp(200)])
    resp = _retry_request(
        send, budget=_RetryBudget(), sleep=sleeps.append, rng=_RecordingRng()
    )
    assert resp.status_code == 200
    assert len(sleeps) == 1  # one backoff between the two attempts


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_does_not_retry_other_4xx(status):
    sleeps: list[float] = []
    send = _scripted_send([_resp(status), _resp(200)])
    resp = _retry_request(
        send, budget=_RetryBudget(), sleep=sleeps.append, rng=_RecordingRng()
    )
    assert resp.status_code == status, "non-retryable 4xx returned as-is"
    assert sleeps == [], "must not sleep/retry on a client error"
    assert send.remaining() == 1, "send called exactly once"


def test_gives_up_after_max_attempts():
    sleeps: list[float] = []
    send = _scripted_send([_resp(503) for _ in range(10)])
    resp = _retry_request(
        send,
        max_attempts=5,
        budget=_RetryBudget(),
        sleep=sleeps.append,
        rng=_RecordingRng(),
    )
    assert resp.status_code == 503
    assert send.remaining() == 5, "exactly max_attempts sends"
    assert len(sleeps) == 4, "backoff between each of the 5 attempts"


def test_honors_retry_after_seconds_over_backoff():
    sleeps: list[float] = []
    send = _scripted_send([_resp(503, {"Retry-After": "7"}), _resp(200)])
    _retry_request(
        send, budget=_RetryBudget(), sleep=sleeps.append, rng=_RecordingRng()
    )
    assert sleeps == [7.0], "Retry-After takes precedence over computed jitter"


def test_honors_retry_after_http_date():
    from email.utils import format_datetime
    from datetime import datetime, timedelta, timezone

    when = datetime.now(timezone.utc) + timedelta(seconds=30)
    send = _scripted_send(
        [_resp(429, {"Retry-After": format_datetime(when)}), _resp(200)]
    )
    sleeps: list[float] = []
    _retry_request(
        send, budget=_RetryBudget(), sleep=sleeps.append, rng=_RecordingRng()
    )
    assert len(sleeps) == 1
    assert 20 <= sleeps[0] <= 31, f"parsed HTTP-date delay out of range: {sleeps[0]}"


def test_full_jitter_ceiling_is_capped_exponential():
    rng = _RecordingRng()
    send = _scripted_send([_resp(500) for _ in range(6)])
    _retry_request(
        send, max_attempts=6, budget=_RetryBudget(), sleep=lambda _s: None, rng=rng
    )
    # ceilings = min(cap, base * 2**(attempt-1)) for attempts 1..5
    ceilings = [hi for (_lo, hi) in rng.calls]
    assert ceilings[0] == pytest.approx(api._RETRY_BASE_DELAY)
    for i in range(1, len(ceilings)):
        assert ceilings[i] <= api._RETRY_MAX_DELAY
        # monotonic non-decreasing, doubling until the cap
        assert ceilings[i] >= ceilings[i - 1]
    assert all(lo == 0 for (lo, _hi) in rng.calls), "full jitter samples from 0"


def test_transport_error_is_retried_then_reraised():
    sleeps: list[float] = []
    send = _scripted_send([httpx.ConnectError("boom"), _resp(200)])
    resp = _retry_request(
        send, budget=_RetryBudget(), sleep=sleeps.append, rng=_RecordingRng()
    )
    assert resp.status_code == 200
    assert len(sleeps) == 1

    send2 = _scripted_send([httpx.ConnectError("boom")] * 5)
    with pytest.raises(httpx.ConnectError):
        _retry_request(
            send2,
            max_attempts=5,
            budget=_RetryBudget(),
            sleep=lambda _s: None,
            rng=_RecordingRng(),
        )


def test_retry_budget_suppresses_retries_when_drained():
    budget = _RetryBudget(max_tokens=10.0, token_ratio=0.1)
    # Drain below the maxTokens/2 threshold.
    for _ in range(6):
        budget.record_failure()
    assert not budget.can_retry()
    sleeps: list[float] = []
    send = _scripted_send([_resp(503), _resp(200)])
    resp = _retry_request(send, budget=budget, sleep=sleeps.append, rng=_RecordingRng())
    assert resp.status_code == 503, "drained budget -> fail fast, no retry"
    assert sleeps == []


def test_retry_budget_token_accounting():
    budget = _RetryBudget(max_tokens=10.0, token_ratio=0.1)
    assert budget.can_retry()  # starts full
    budget.record_failure()  # 9
    assert budget.can_retry()
    for _ in range(4):
        budget.record_failure()  # 5 -> not > threshold (5)
    assert not budget.can_retry()
    for _ in range(100):
        budget.record_success()  # refills, capped at max_tokens
    assert budget.can_retry()


# ---------------------------------------------------------------------------
# Wiring: init + complete retried; sweep NOT retried
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code, json_data=None, headers=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._json


class _FakeClient:
    def __init__(self, scripts: dict[str, list]):
        self._scripts = scripts
        self.calls: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, **kwargs):
        self.calls.append(url)
        for suffix, responses in self._scripts.items():
            if url.endswith(suffix):
                return responses.pop(0)
        raise AssertionError(f"unexpected POST {url}")


def test_upload_task_retries_init_and_complete(monkeypatch, tmp_path):
    task_dir = tmp_path / "t"
    task_dir.mkdir()
    (task_dir / "instruction.md").write_text("do")

    monkeypatch.setattr(api, "validate_task_timeout_config", lambda *_a, **_k: None)
    monkeypatch.setattr(api, "compute_task_content_hash", lambda *_a, **_k: "hash")
    fake_tar = tmp_path / "pkg" / "t.tar.gz"
    fake_tar.parent.mkdir()
    fake_tar.write_text("x")
    monkeypatch.setattr(api, "archive_task_dir", lambda *_a, **_k: fake_tar)
    monkeypatch.setattr(api, "get_auth_headers", lambda: {})
    monkeypatch.setattr(api, "_upload_to_presigned_url", lambda *_a, **_k: None)

    init_ok = _FakeResp(
        200,
        {
            "task_id": "T1",
            "name": "t",
            "version": 1,
            "upload_url": "https://s3/put",
            "upload_headers": {},
        },
    )
    complete_ok = _FakeResp(200, {"task_id": "T1"})
    fake = _FakeClient(
        {
            "/tasks/upload/init": [_FakeResp(503), init_ok],
            "/tasks/upload/complete": [_FakeResp(500), complete_ok],
        }
    )
    monkeypatch.setattr(api.httpx, "Client", lambda *_a, **_k: fake)
    monkeypatch.setattr(api.time, "sleep", lambda *_a: None)

    result = api.upload_task("http://api", task_dir)

    assert result == {"task_id": "T1"}
    assert fake.calls.count("http://api/tasks/upload/init") == 2  # retried once
    assert fake.calls.count("http://api/tasks/upload/complete") == 2  # retried once


def test_submit_sweep_is_not_retried(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("_retry_request must not wrap /tasks/sweep")

    monkeypatch.setattr(api, "_retry_request", _boom)
    monkeypatch.setattr(api, "get_auth_headers", lambda: {})
    fake = _FakeClient({"/tasks/sweep": [_FakeResp(500, text="overloaded")]})
    monkeypatch.setattr(api.httpx, "Client", lambda *_a, **_k: fake)

    with pytest.raises(typer.Exit):
        submit_sweep(
            "http://api",
            "T1",
            [],
            None,  # environment
            None,  # user
            "low",  # priority
            None,  # experiment_id
        )

    assert fake.calls == ["http://api/tasks/sweep"], "sweep posted exactly once"

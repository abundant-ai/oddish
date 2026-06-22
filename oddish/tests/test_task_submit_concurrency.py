"""Wiring tests for adaptive task-submission concurrency.

Covers the runtime pieces layered on top of the AIMD limiter and how they wire
into the upload/submit pools:

* ``resolve_submit_concurrency`` / ``resolve_s3_put_concurrency`` -- env + flag
  precedence and clamping (the ``--submit-concurrency`` flag and
  ``ODDISH_TASK_UPLOAD_CONCURRENCY`` / ``ODDISH_TASK_S3_UPLOAD_CONCURRENCY`` env
  vars resolve through these).
* ``ConcurrencyGate`` -- in-flight throttle + success/backpressure feedback,
  including reported HTTP statuses, API-only latency, and neutral failures.
* ``map_with_adaptive_concurrency`` -- order preservation + honoring the limit.
* The upload pool (``upload_tasks_with_progress``) honoring a pinned value via
  flag and via env.
* The S3 presigned-PUT step being bounded by its own, separate semaphore and
  kept out of the API latency signal.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
import typer

import oddish.cli.api as api
from oddish.cli._concurrency import (
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_MIN_CONCURRENCY,
    DEFAULT_S3_PUT_CONCURRENCY,
    S3_PUT_CONCURRENCY_ENV_VAR,
    SUBMIT_CONCURRENCY_ENV_VAR,
    AdaptiveConcurrencyLimiter,
    ConcurrencyGate,
    map_with_adaptive_concurrency,
    report_api_call,
    report_backpressure,
    resolve_s3_put_concurrency,
    resolve_submit_concurrency,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _PeakRecorder:
    """Records the peak number of workers running concurrently.

    Each call blocks on a barrier sized to ``parties`` so exactly that many
    callers are forced to overlap -- if the throttle is tighter than ``parties``
    the barrier times out and the worker raises (a loud failure, not a hang).
    """

    def __init__(self, parties: int, *, timeout: float = 5.0) -> None:
        self.active = 0
        self.peak = 0
        self._lock = threading.Lock()
        self._barrier = threading.Barrier(parties, timeout=timeout)

    def hit(self) -> None:
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        self._barrier.wait()
        with self._lock:
            self.active -= 1


class _RecordingSemaphore:
    """Stand-in context manager that counts acquire/release + peak holders."""

    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0
        self.active = 0
        self.peak = 0
        self._lock = threading.Lock()

    def __enter__(self) -> _RecordingSemaphore:
        with self._lock:
            self.entered += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
        return self

    def __exit__(self, *exc: object) -> bool:
        with self._lock:
            self.exited += 1
            self.active -= 1
        return False


class _FakeS3Client:
    """Minimal httpx.Client stand-in whose PUT always returns 200."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def __enter__(self) -> _FakeS3Client:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def put(self, url: str, *, headers=None, content=None) -> httpx.Response:
        if hasattr(content, "read"):
            content.read()
        return httpx.Response(status_code=200)


def _raise(exc: BaseException):
    def _fn():
        raise exc

    return _fn


class _FakeStatusClient:
    """httpx.Client stand-in whose POST returns a fixed status."""

    def __init__(self, status: int) -> None:
        self._status = status

    def __enter__(self) -> _FakeStatusClient:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def post(self, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(status_code=self._status, text="overloaded")


def _resp(status: int) -> httpx.Response:
    return httpx.Response(status_code=status)


def _scripted_send(responses):
    """Zero-arg send() that yields the given responses in order."""
    seq = list(responses)

    def send():
        return seq.pop(0)

    return send


# ---------------------------------------------------------------------------
# resolve_submit_concurrency: env + flag precedence
# ---------------------------------------------------------------------------


def test_default_is_adaptive(monkeypatch):
    monkeypatch.delenv(SUBMIT_CONCURRENCY_ENV_VAR, raising=False)
    limiter = resolve_submit_concurrency()
    assert limiter.min_limit == DEFAULT_MIN_CONCURRENCY
    assert limiter.max_limit == DEFAULT_MAX_CONCURRENCY
    assert limiter.limit == DEFAULT_MIN_CONCURRENCY


def test_explicit_value_pins_limit(monkeypatch):
    monkeypatch.delenv(SUBMIT_CONCURRENCY_ENV_VAR, raising=False)
    limiter = resolve_submit_concurrency(7)
    assert limiter.min_limit == limiter.max_limit == 7
    assert limiter.limit == 7
    # Pinned -> no adaptation.
    limiter.record_sample(0.1, in_flight=99)
    limiter.record_backpressure()
    assert limiter.limit == 7


def test_env_var_pins_limit(monkeypatch):
    monkeypatch.setenv(SUBMIT_CONCURRENCY_ENV_VAR, "5")
    limiter = resolve_submit_concurrency()
    assert limiter.limit == 5
    assert limiter.min_limit == limiter.max_limit == 5


def test_explicit_value_overrides_env(monkeypatch):
    monkeypatch.setenv(SUBMIT_CONCURRENCY_ENV_VAR, "9")
    limiter = resolve_submit_concurrency(3)
    assert limiter.limit == 3


def test_malformed_env_falls_back_to_adaptive(monkeypatch):
    monkeypatch.setenv(SUBMIT_CONCURRENCY_ENV_VAR, "not-a-number")
    limiter = resolve_submit_concurrency()
    assert limiter.min_limit == DEFAULT_MIN_CONCURRENCY
    assert limiter.max_limit == DEFAULT_MAX_CONCURRENCY


# ---------------------------------------------------------------------------
# resolve_s3_put_concurrency: separate, smaller, clamped bound
# ---------------------------------------------------------------------------


def test_s3_default(monkeypatch):
    monkeypatch.delenv(S3_PUT_CONCURRENCY_ENV_VAR, raising=False)
    assert resolve_s3_put_concurrency() == DEFAULT_S3_PUT_CONCURRENCY == 4


def test_s3_env_override(monkeypatch):
    monkeypatch.setenv(S3_PUT_CONCURRENCY_ENV_VAR, "2")
    assert resolve_s3_put_concurrency() == 2


def test_s3_clamps_into_small_band(monkeypatch):
    monkeypatch.delenv(S3_PUT_CONCURRENCY_ENV_VAR, raising=False)
    assert resolve_s3_put_concurrency(99) == 6  # ceiling
    assert resolve_s3_put_concurrency(0) == 1  # floor


# ---------------------------------------------------------------------------
# ConcurrencyGate
# ---------------------------------------------------------------------------


def test_gate_grows_limit_on_clean_low_latency_samples():
    # A run of clean, fast API calls feeds the gradient and grows the limit
    # (gradient ~1.0 when noload == actual), while in_flight pushes the ceiling.
    limiter = AdaptiveConcurrencyLimiter(min_limit=2, max_limit=16, initial_limit=2)
    gate = ConcurrencyGate(limiter)

    def worker():
        report_api_call(0.01)
        return "ok"

    for _ in range(6):
        assert gate.run(worker) == "ok"
    assert limiter.limit > 2


def test_gate_backs_off_on_timeout():
    limiter = AdaptiveConcurrencyLimiter(min_limit=2, max_limit=16, initial_limit=8)
    gate = ConcurrencyGate(limiter)
    with pytest.raises(httpx.ReadTimeout):
        gate.run(_raise(httpx.ReadTimeout("slow")))
    assert limiter.limit < 8


def test_gate_backs_off_on_pool_timeout():
    limiter = AdaptiveConcurrencyLimiter(min_limit=2, max_limit=16, initial_limit=8)
    gate = ConcurrencyGate(limiter)
    with pytest.raises(httpx.PoolTimeout):
        gate.run(_raise(httpx.PoolTimeout("checkout")))
    assert limiter.limit < 8


def test_gate_treats_non_load_failure_as_neutral():
    # A non-load failure (a deterministic 4xx surfaced as an exception, or a bug)
    # must be neutral: it neither shrinks the limit nor counts as a success that
    # grows it -- even when utilization would otherwise allow growth.
    limiter = AdaptiveConcurrencyLimiter(min_limit=2, max_limit=16, initial_limit=2)
    gate = ConcurrencyGate(limiter)
    with pytest.raises(ValueError):
        gate.run(_raise(ValueError("bug")))
    assert limiter.limit == 2  # in_flight*2 >= limit, yet no growth


def test_gate_backs_off_on_reported_status_then_success():
    # A transient status reported during the call shrinks the limit even though
    # the call ultimately returns (e.g. a 503 that a retry recovered from).
    limiter = AdaptiveConcurrencyLimiter(min_limit=2, max_limit=16, initial_limit=8)
    gate = ConcurrencyGate(limiter)

    def worker():
        report_api_call(0.01, backpressure=True)
        return "done"

    assert gate.run(worker) == "done"
    assert limiter.limit < 8


def test_gate_backs_off_on_reported_status_then_exit():
    # upload_task / submit_sweep report backpressure, then raise typer.Exit on a
    # 5xx; the limit must shrink despite the swallowed status.
    limiter = AdaptiveConcurrencyLimiter(min_limit=2, max_limit=16, initial_limit=8)
    gate = ConcurrencyGate(limiter)

    def worker():
        report_backpressure()
        raise typer.Exit(1)

    with pytest.raises(typer.Exit):
        gate.run(worker)
    assert limiter.limit < 8


def test_gate_observes_only_reported_api_latency():
    # The RTT signal is built from reported API time, never wall-clock time, so a
    # slow S3 PUT inside the worker can't feed the gradient.
    limiter = resolve_submit_concurrency(8)
    gate = ConcurrencyGate(limiter)

    def worker():
        report_api_call(0.05)  # API-only time; any S3-PUT time is never reported
        return "ok"

    gate.run(worker)
    assert limiter.rtt_actual == pytest.approx(0.05)


def test_report_helpers_are_noop_outside_gate():
    # Reporting outside any gate slot must not raise.
    report_backpressure()
    report_api_call(0.01, backpressure=True)


def test_gate_caps_in_flight_below_pool_size():
    # Pool is larger than the limit; the gate (not the pool) is the throttle.
    limiter = AdaptiveConcurrencyLimiter(min_limit=2, max_limit=2, initial_limit=2)
    gate = ConcurrencyGate(limiter)
    recorder = _PeakRecorder(parties=2)
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(gate.run, recorder.hit) for _ in range(8)]
        for future in futures:
            future.result()
    assert recorder.peak == 2


# ---------------------------------------------------------------------------
# map_with_adaptive_concurrency
# ---------------------------------------------------------------------------


def test_map_preserves_input_order():
    limiter = resolve_submit_concurrency(4)
    assert map_with_adaptive_concurrency(
        [1, 2, 3, 4, 5], lambda x: x * 10, limiter
    ) == [
        10,
        20,
        30,
        40,
        50,
    ]


def test_map_empty_returns_empty():
    assert (
        map_with_adaptive_concurrency([], lambda x: x, resolve_submit_concurrency())
        == []
    )


def test_map_calls_on_complete_per_item():
    seen = []
    map_with_adaptive_concurrency(
        [1, 2, 3],
        lambda x: x,
        resolve_submit_concurrency(2),
        on_complete=lambda: seen.append(1),
    )
    assert len(seen) == 3


def test_map_honors_pinned_concurrency():
    # The shared primitive both pools use -- a pinned limit caps real overlap.
    limiter = resolve_submit_concurrency(3)
    recorder = _PeakRecorder(parties=3)

    def worker(item):
        recorder.hit()
        return item

    results = map_with_adaptive_concurrency(list(range(12)), worker, limiter)
    assert sorted(results) == list(range(12))
    assert recorder.peak == 3


# ---------------------------------------------------------------------------
# upload pool honors the resolved concurrency (flag + env)
# ---------------------------------------------------------------------------


def test_upload_pool_honors_pinned_flag(monkeypatch):
    monkeypatch.delenv(SUBMIT_CONCURRENCY_ENV_VAR, raising=False)
    recorder = _PeakRecorder(parties=3)

    def fake_upload_task(api_url, task_path, **kwargs):
        recorder.hit()
        return {"task_id": str(task_path), "name": task_path.name}

    monkeypatch.setattr(api, "upload_task", fake_upload_task)
    task_paths = [Path(f"task-{i}") for i in range(12)]
    results = api.upload_tasks_with_progress(
        "http://api", task_paths, register=False, quiet=True, concurrency=3
    )
    assert len(results) == 12
    assert recorder.peak == 3


def test_upload_pool_honors_env(monkeypatch):
    monkeypatch.setenv(SUBMIT_CONCURRENCY_ENV_VAR, "2")
    recorder = _PeakRecorder(parties=2)

    def fake_upload_task(api_url, task_path, **kwargs):
        recorder.hit()
        return {"task_id": str(task_path)}

    monkeypatch.setattr(api, "upload_task", fake_upload_task)
    task_paths = [Path(f"task-{i}") for i in range(8)]
    api.upload_tasks_with_progress("http://api", task_paths, register=False, quiet=True)
    assert recorder.peak == 2


# ---------------------------------------------------------------------------
# S3 presigned PUT is bounded by its own separate semaphore
# ---------------------------------------------------------------------------


def test_s3_put_acquires_separate_semaphore(monkeypatch, tmp_path):
    sem = _RecordingSemaphore()
    monkeypatch.setattr(api, "_get_s3_put_semaphore", lambda: sem)
    monkeypatch.setattr(api.httpx, "Client", _FakeS3Client)

    tarball = tmp_path / "task.tar.gz"
    tarball.write_bytes(b"payload")

    api._upload_to_presigned_url("http://s3/put", tarball, {})

    assert sem.entered == 1
    assert sem.exited == 1


def test_s3_put_excluded_from_api_latency_signal(monkeypatch, tmp_path):
    # The S3 PUT must report no API call, so its (potentially slow) wall-time
    # never feeds the gradient's RTT signal.
    monkeypatch.setattr(api, "_get_s3_put_semaphore", _RecordingSemaphore)
    monkeypatch.setattr(api.httpx, "Client", _FakeS3Client)
    limiter = resolve_submit_concurrency(4)
    gate = ConcurrencyGate(limiter)
    tarball = tmp_path / "task.tar.gz"
    tarball.write_bytes(b"payload")

    gate.run(lambda: api._upload_to_presigned_url("http://s3/put", tarball, {}))

    assert limiter.rtt_actual is None  # no API call observed -> no RTT sample


# ---------------------------------------------------------------------------
# HTTP-status backpressure reaches the limiter through the API layer
# ---------------------------------------------------------------------------


def test_retry_request_reports_backpressure_within_gate():
    # init/complete go through _retry_request; a 503 it recovers from must still
    # shrink the in-flight limit.
    limiter = AdaptiveConcurrencyLimiter(min_limit=2, max_limit=16, initial_limit=8)
    gate = ConcurrencyGate(limiter)

    def worker():
        send = _scripted_send([_resp(503), _resp(200)])
        return api._retry_request(
            send, budget=api._RetryBudget(), sleep=lambda _s: None
        )

    resp = gate.run(worker)
    assert resp.status_code == 200
    assert limiter.limit < 8


def test_submit_sweep_reports_backpressure_status(monkeypatch):
    # submit_sweep does a single un-retried POST; a 503 must shrink the limit
    # before the typer.Exit it raises.
    limiter = AdaptiveConcurrencyLimiter(min_limit=2, max_limit=16, initial_limit=8)
    gate = ConcurrencyGate(limiter)
    monkeypatch.setattr(api, "get_auth_headers", lambda: {})
    monkeypatch.setattr(api.httpx, "Client", lambda *a, **k: _FakeStatusClient(503))

    def worker():
        return api.submit_sweep("http://api", "t1", [{}], None, None, "normal", None)

    with pytest.raises(typer.Exit):
        gate.run(worker)
    assert limiter.limit < 8

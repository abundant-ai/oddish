"""Wiring tests for adaptive task-submission concurrency.

Covers the runtime pieces layered on top of the AIMD limiter and how they wire
into the upload/submit pools:

* ``resolve_submit_concurrency`` / ``resolve_s3_put_concurrency`` -- env + flag
  precedence and clamping (the ``--submit-concurrency`` flag and
  ``ODDISH_TASK_UPLOAD_CONCURRENCY`` / ``ODDISH_TASK_S3_UPLOAD_CONCURRENCY`` env
  vars resolve through these).
* ``ConcurrencyGate`` -- in-flight throttle + success/backpressure feedback.
* ``map_with_adaptive_concurrency`` -- order preservation + honoring the limit.
* The upload pool (``upload_tasks_with_progress``) honoring a pinned value via
  flag and via env.
* The S3 presigned-PUT step being bounded by its own, separate semaphore.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

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
    limiter.record_success(in_flight=99)
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


def test_gate_grows_limit_on_clean_success():
    limiter = AdaptiveConcurrencyLimiter(min_limit=2, max_limit=16, initial_limit=2)
    gate = ConcurrencyGate(limiter)
    assert gate.run(lambda: "ok") == "ok"
    # in_flight == 1 at acquire, 1*2 >= 2 -> grow.
    assert limiter.limit == 3


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


def test_gate_does_not_back_off_on_client_error():
    limiter = AdaptiveConcurrencyLimiter(min_limit=4, max_limit=16, initial_limit=8)
    gate = ConcurrencyGate(limiter)
    with pytest.raises(ValueError):
        gate.run(_raise(ValueError("bug")))
    assert limiter.limit == 8  # neutral outcome, util-gated -> unchanged


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

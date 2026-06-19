"""Adaptive client-side concurrency control for task submission.

The CLI uploads and submits batches of tasks against a shared API. A fixed
worker count is a poor fit: too low and large batches crawl, too high and a
busy API starts shedding load. This module provides an additive-increase /
multiplicative-decrease (AIMD) in-flight limiter that discovers a safe
operating point on its own -- it raises its ceiling by one on each clean
success and backs off multiplicatively when the server signals backpressure
(HTTP 429/500/502/503/504, request timeouts, a slow connection-pool checkout,
or sustained high latency).

The design mirrors Netflix's ``concurrency-limits`` ``AIMDLimit`` (gentle 0.9
backoff with a hard 0.5x floor, a utilization-gated increase, and a small
``[min, max]`` clamp), scaled down to a bursty CLI workload. The clamp acts as
a Little's-Law guardrail (in-flight ~= throughput x latency); AIMD finds the
operating point inside it rather than pinning a hand-computed value.

The pieces here are pure -- no network calls, no thread pools -- so the control
logic is fully unit-testable. ``ConcurrencyGate`` / ``map_with_adaptive_concurrency``
(added alongside the pool wiring) build the runtime throttle on top.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TypeVar, cast

import httpx

# Bounds for the adaptive in-flight limit. Small bounds are deliberate: a floor
# of 4 keeps throughput off the floor for large batches, while a ceiling of 16
# caps the blast radius on a shared API. (Connection-pool sizing lore -- e.g.
# HikariCP -- favors small pools; this is the same instinct.)
DEFAULT_MIN_CONCURRENCY = 4
DEFAULT_MAX_CONCURRENCY = 16

# Gentle multiplicative-decrease factor. 0.9 maximizes throughput for a single
# client protecting a downstream (a deep cut takes many +1 successes to climb
# back); 0.5 is the hard floor so one backpressure step never cuts the limit by
# more than half. This matches AIMDLimit's enforced ``[0.5, 1.0)`` backoff range.
DEFAULT_BACKOFF_FACTOR = 0.9
MIN_BACKOFF_FACTOR = 0.5

# Transient statuses that signal server backpressure. Identical to the retry set
# used for the idempotent upload calls (api._RETRY_STATUS_CODES). Deterministic
# 4xx (400/401/403/404/409/422) are client errors -- replaying or backing off
# won't help -- so they must NOT shrink the limit.
BACKPRESSURE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

# Env override for the adaptive API limit. An integer pins the limit (disables
# adaptation); unset / non-numeric leaves it adaptive. The ``--submit-concurrency``
# CLI flag takes precedence over this.
SUBMIT_CONCURRENCY_ENV_VAR = "ODDISH_TASK_UPLOAD_CONCURRENCY"

# A separate, smaller ceiling for the S3 presigned-PUT step. The object store is
# a different service from the API, so its upload concurrency is bounded
# independently and is never fed by (and never feeds) the API backpressure
# signal. Clamped to a small band so it stays well under the API limit.
DEFAULT_S3_PUT_CONCURRENCY = 4
MIN_S3_PUT_CONCURRENCY = 1
MAX_S3_PUT_CONCURRENCY = 6
S3_PUT_CONCURRENCY_ENV_VAR = "ODDISH_TASK_S3_UPLOAD_CONCURRENCY"

T = TypeVar("T")
R = TypeVar("R")


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class AdaptiveConcurrencyLimiter:
    """An AIMD in-flight limiter.

    ``record_success`` adds one to the limit (additive increase); ``record_backpressure``
    multiplies it by ``backoff_factor`` (multiplicative decrease). Both clamp the
    result to ``[min_limit, max_limit]``. The limit is held as a float internally
    so repeated decreases don't get stuck on integer rounding, and exposed as an
    int via :attr:`limit`.

    Setting ``min_limit == max_limit`` pins the limit (no adaptation) -- used when
    a caller supplies an explicit value.
    """

    def __init__(
        self,
        *,
        min_limit: int = DEFAULT_MIN_CONCURRENCY,
        max_limit: int = DEFAULT_MAX_CONCURRENCY,
        initial_limit: int | None = None,
        backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    ) -> None:
        if min_limit < 1:
            raise ValueError(f"min_limit must be >= 1, got {min_limit}")
        if max_limit < min_limit:
            raise ValueError(
                f"max_limit ({max_limit}) must be >= min_limit ({min_limit})"
            )
        self.min_limit = int(min_limit)
        self.max_limit = int(max_limit)
        # Clamp the multiplier into [0.5, 1.0]: floor at 0.5 so a single step is
        # never more aggressive than a halving; cap below 1.0 so a decrease always
        # decreases.
        self.backoff_factor = _clamp(float(backoff_factor), MIN_BACKOFF_FACTOR, 0.999)
        start = self.min_limit if initial_limit is None else int(initial_limit)
        self._limit = float(_clamp(float(start), self.min_limit, self.max_limit))
        # Reads/writes happen from worker threads in the runtime gate; the lock
        # keeps the float state consistent (it guards arithmetic, not any I/O).
        self._lock = threading.Lock()

    def _effective_limit(self) -> int:
        return int(_clamp(self._limit, float(self.min_limit), float(self.max_limit)))

    @property
    def limit(self) -> int:
        """The current in-flight ceiling, clamped to ``[min_limit, max_limit]``."""
        with self._lock:
            return self._effective_limit()

    def record_success(self, *, in_flight: int | None = None) -> int:
        """Register a clean success and additively increase the limit.

        When ``in_flight`` is supplied the increase is gated on utilization
        (``in_flight * 2 >= limit``), so the ceiling doesn't creep upward while
        the client isn't actually pushing against it. Returns the new limit.
        """
        with self._lock:
            if in_flight is None or in_flight * 2 >= self._effective_limit():
                self._limit = min(float(self.max_limit), self._limit + 1.0)
            return self._effective_limit()

    def record_backpressure(self) -> int:
        """Register backpressure and multiplicatively decrease the limit.

        Returns the new limit.
        """
        with self._lock:
            self._limit = max(float(self.min_limit), self._limit * self.backoff_factor)
            return self._effective_limit()


def classify_backpressure(
    *,
    status_code: int | None = None,
    exception: BaseException | None = None,
    latency: float | None = None,
    slow_threshold: float | None = None,
) -> bool:
    """Decide whether a request outcome should shrink the in-flight limit.

    Backpressure is signalled by a transient status (:data:`BACKPRESSURE_STATUS_CODES`),
    a request timeout or a slow connection-pool checkout (``httpx.TimeoutException``,
    which covers ``httpx.PoolTimeout``), or a latency sample above ``slow_threshold``.
    Everything else -- deterministic 4xx, non-load transport errors, latency within
    threshold -- is treated as neutral, so the limit isn't cut for problems extra
    concurrency control can't fix.
    """
    if exception is not None:
        # TimeoutException covers ConnectTimeout/ReadTimeout/WriteTimeout and
        # PoolTimeout (the connection pool itself is saturated == too much
        # in-flight). Other transport errors (e.g. ConnectError) aren't
        # necessarily load-driven, so they don't trigger a backoff.
        return isinstance(exception, httpx.TimeoutException)
    if status_code is not None and status_code in BACKPRESSURE_STATUS_CODES:
        return True
    if latency is not None and slow_threshold is not None and latency > slow_threshold:
        return True
    return False


class _LatencyMonitor:
    """EWMA of request latency, used to derive the high-latency backpressure cut.

    A single GC pause or network blip shouldn't cause an outsized reduction, so
    the baseline is an exponentially-weighted moving average and only samples a
    multiple above it (after a short warmup) count as slow. ``slow_threshold`` is
    meant to be read *before* folding in the current sample, so a request is
    compared against the baseline of the requests that preceded it.
    """

    def __init__(
        self,
        *,
        alpha: float = 0.2,
        slow_multiplier: float = 3.0,
        warmup: int = 4,
    ) -> None:
        self._alpha = alpha
        self._slow_multiplier = slow_multiplier
        self._warmup = warmup
        self._count = 0
        self._ewma = 0.0
        self._lock = threading.Lock()

    def slow_threshold(self) -> float | None:
        """Current slow-latency cutoff, or ``None`` until warmed up."""
        with self._lock:
            if self._count < self._warmup:
                return None
            return self._ewma * self._slow_multiplier

    def observe(self, sample: float) -> None:
        """Fold a latency sample (seconds) into the moving average."""
        with self._lock:
            if self._count == 0:
                self._ewma = sample
            else:
                self._ewma = self._alpha * sample + (1.0 - self._alpha) * self._ewma
            self._count += 1


class ConcurrencyGate:
    """Throttle concurrent work to a limiter's adaptive in-flight ceiling.

    Wrap each unit of work with :meth:`run`: it blocks until in-flight is below
    the current limit, runs the callable, times it, and feeds the outcome
    (success vs. backpressure) back to the limiter. Built to drive a
    ``ThreadPoolExecutor`` sized to ``limiter.max_limit`` -- the gate, not the
    pool size, is the real throttle, so the limit can shrink below the pool size
    under load.
    """

    def __init__(
        self,
        limiter: AdaptiveConcurrencyLimiter,
        *,
        clock: Callable[[], float] = time.monotonic,
        latency_monitor: _LatencyMonitor | None = None,
    ) -> None:
        self._limiter = limiter
        self._clock = clock
        self._latency = (
            latency_monitor if latency_monitor is not None else _LatencyMonitor()
        )
        self._cond = threading.Condition()
        self._in_flight = 0

    @property
    def in_flight(self) -> int:
        with self._cond:
            return self._in_flight

    def _acquire(self) -> int:
        with self._cond:
            while self._in_flight >= self._limiter.limit:
                self._cond.wait()
            self._in_flight += 1
            return self._in_flight

    def _release(self) -> None:
        with self._cond:
            self._in_flight -= 1
            # The limit may have changed (grown or shrunk); wake every waiter so
            # they re-check against the current value.
            self._cond.notify_all()

    def run(self, fn: Callable[..., R], /, *args: object, **kwargs: object) -> R:
        """Run ``fn(*args, **kwargs)`` once a slot is free, recording the outcome."""
        in_flight = self._acquire()
        start = self._clock()
        error: BaseException | None = None
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - reclassified for the limiter, then re-raised
            error = exc
            raise
        finally:
            latency = self._clock() - start
            backpressure = classify_backpressure(
                exception=error,
                latency=latency,
                slow_threshold=self._latency.slow_threshold(),
            )
            if backpressure:
                self._limiter.record_backpressure()
            else:
                self._limiter.record_success(in_flight=in_flight)
            self._latency.observe(latency)
            self._release()


def map_with_adaptive_concurrency(
    items: Sequence[T],
    worker: Callable[[T], R],
    limiter: AdaptiveConcurrencyLimiter,
    *,
    gate: ConcurrencyGate | None = None,
    on_complete: Callable[[], None] | None = None,
) -> list[R]:
    """Apply ``worker`` to each item, throttled to the limiter's in-flight limit.

    Results are returned in input order. ``on_complete`` (if given) fires once
    per finished item -- handy for advancing a progress bar. The thread pool is
    sized to ``limiter.max_limit``; the :class:`ConcurrencyGate` does the actual
    throttling, so an adaptive limit shrinks real concurrency below the pool
    size under backpressure.
    """
    if not items:
        return []
    gate = gate if gate is not None else ConcurrencyGate(limiter)
    results: list[R | None] = [None] * len(items)
    max_workers = min(limiter.max_limit, len(items))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {
            executor.submit(gate.run, worker, item): index
            for index, item in enumerate(items)
        }
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            results[index] = future.result()
            if on_complete is not None:
                on_complete()
    return cast("list[R]", results)


def _read_int_env(env_var: str) -> int | None:
    """Read an int from ``env_var``; return ``None`` if unset/blank/non-numeric."""
    raw = os.environ.get(env_var)
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def resolve_submit_concurrency(
    explicit: int | None = None,
    *,
    env_var: str = SUBMIT_CONCURRENCY_ENV_VAR,
    min_limit: int = DEFAULT_MIN_CONCURRENCY,
    max_limit: int = DEFAULT_MAX_CONCURRENCY,
) -> AdaptiveConcurrencyLimiter:
    """Resolve the in-flight limiter for API-bound submit/upload work.

    Precedence: ``explicit`` (the ``--submit-concurrency`` flag) > ``env_var`` >
    adaptive default. An explicit value (flag or env) *pins* the limit
    (``min == max``, no adaptation); the default returns an AIMD limiter clamped
    to ``[min_limit, max_limit]`` and starting at the floor.
    """
    pinned = explicit if explicit is not None else _read_int_env(env_var)
    if pinned is not None:
        pinned = max(1, int(pinned))
        return AdaptiveConcurrencyLimiter(
            min_limit=pinned, max_limit=pinned, initial_limit=pinned
        )
    return AdaptiveConcurrencyLimiter(
        min_limit=min_limit, max_limit=max_limit, initial_limit=min_limit
    )


def resolve_s3_put_concurrency(
    explicit: int | None = None,
    *,
    env_var: str = S3_PUT_CONCURRENCY_ENV_VAR,
) -> int:
    """Resolve the bound for concurrent S3 presigned PUTs.

    Precedence: ``explicit`` > ``env_var`` > :data:`DEFAULT_S3_PUT_CONCURRENCY`,
    clamped to ``[MIN_S3_PUT_CONCURRENCY, MAX_S3_PUT_CONCURRENCY]``. This bound is
    independent of the adaptive API limiter.
    """
    value = explicit if explicit is not None else _read_int_env(env_var)
    if value is None:
        value = DEFAULT_S3_PUT_CONCURRENCY
    return int(_clamp(int(value), MIN_S3_PUT_CONCURRENCY, MAX_S3_PUT_CONCURRENCY))

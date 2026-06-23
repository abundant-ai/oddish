"""Unit tests for the pure adaptive-concurrency primitives.

``AdaptiveConcurrencyLimiter`` is an additive-increase / multiplicative-decrease
(AIMD) in-flight limiter: it raises its ceiling by one on a clean success and
backs off multiplicatively when the server signals backpressure, clamped to a
small ``[min, max]`` band. ``classify_backpressure`` decides what counts as
backpressure (transient statuses, timeouts / slow pool checkout, high latency),
and ``_LatencyMonitor`` supplies the EWMA baseline for the latency signal. All
three are pure (no network, no thread pools) and tested in isolation here.
"""

from __future__ import annotations

import httpx
import pytest

from oddish.cli._concurrency import (
    BACKPRESSURE_STATUS_CODES,
    DEFAULT_BACKOFF_FACTOR,
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_MIN_CONCURRENCY,
    MIN_BACKOFF_FACTOR,
    AdaptiveConcurrencyLimiter,
    _LatencyMonitor,
    classify_backpressure,
)

# ---------------------------------------------------------------------------
# AdaptiveConcurrencyLimiter: additive increase
# ---------------------------------------------------------------------------


def test_default_limiter_starts_at_floor():
    limiter = AdaptiveConcurrencyLimiter()
    assert limiter.min_limit == DEFAULT_MIN_CONCURRENCY == 4
    assert limiter.max_limit == DEFAULT_MAX_CONCURRENCY == 16
    assert limiter.limit == DEFAULT_MIN_CONCURRENCY


def test_success_increases_limit_by_one():
    limiter = AdaptiveConcurrencyLimiter(min_limit=4, max_limit=16)
    assert limiter.limit == 4
    assert limiter.record_success() == 5
    assert limiter.record_success() == 6
    assert limiter.limit == 6


def test_success_clamps_at_max():
    limiter = AdaptiveConcurrencyLimiter(min_limit=4, max_limit=6)
    for _ in range(20):
        limiter.record_success()
    assert limiter.limit == 6


def test_utilization_gated_increase_when_in_flight_reported():
    # Netflix-style anti-creep: only grow when in-flight pushes the limit
    # (in_flight * 2 >= limit). Below that the cap must not drift upward.
    limiter = AdaptiveConcurrencyLimiter(min_limit=4, max_limit=16)
    assert limiter.limit == 4
    # in_flight=1 -> 1*2=2 < 4: no growth.
    assert limiter.record_success(in_flight=1) == 4
    # in_flight=2 -> 2*2=4 >= 4: grow.
    assert limiter.record_success(in_flight=2) == 5


# ---------------------------------------------------------------------------
# AdaptiveConcurrencyLimiter: multiplicative decrease + clamp
# ---------------------------------------------------------------------------


def test_backpressure_multiplies_by_backoff_factor():
    assert DEFAULT_BACKOFF_FACTOR == pytest.approx(0.9)
    limiter = AdaptiveConcurrencyLimiter(min_limit=4, max_limit=16, initial_limit=16)
    # 16 * 0.9 = 14.4 -> 14 (a single step drops by at least one above the floor).
    assert limiter.record_backpressure() == 14
    assert limiter.limit <= 15


def test_backpressure_clamps_at_floor():
    limiter = AdaptiveConcurrencyLimiter(min_limit=4, max_limit=16, initial_limit=5)
    for _ in range(20):
        limiter.record_backpressure()
    assert limiter.limit == 4


def test_backoff_factor_floored_at_half():
    # An over-aggressive multiplier is floored at 0.5 so a single backpressure
    # step never cuts the limit by more than half (Netflix's [0.5, 1.0) range).
    limiter = AdaptiveConcurrencyLimiter(
        min_limit=1, max_limit=16, initial_limit=16, backoff_factor=0.1
    )
    assert limiter.backoff_factor == MIN_BACKOFF_FACTOR == 0.5
    assert limiter.record_backpressure() == 8  # 16 * 0.5, not 16 * 0.1


def test_alternating_success_and_backpressure_converges_within_band():
    limiter = AdaptiveConcurrencyLimiter(min_limit=4, max_limit=16)
    for _ in range(50):
        limiter.record_success()
        limiter.record_backpressure()
        assert 4 <= limiter.limit <= 16


# ---------------------------------------------------------------------------
# AdaptiveConcurrencyLimiter: pinned + validation
# ---------------------------------------------------------------------------


def test_pinned_limiter_never_moves():
    # min == max pins the limit: explicit values must not adapt.
    limiter = AdaptiveConcurrencyLimiter(min_limit=7, max_limit=7, initial_limit=7)
    assert limiter.limit == 7
    limiter.record_success()
    limiter.record_success(in_flight=99)
    assert limiter.limit == 7
    limiter.record_backpressure()
    assert limiter.limit == 7


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_limit": 0},
        {"min_limit": -1},
        {"min_limit": 8, "max_limit": 4},
    ],
)
def test_invalid_bounds_raise(kwargs):
    with pytest.raises(ValueError):
        AdaptiveConcurrencyLimiter(**kwargs)


# ---------------------------------------------------------------------------
# classify_backpressure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", sorted(BACKPRESSURE_STATUS_CODES))
def test_transient_statuses_are_backpressure(status):
    assert classify_backpressure(status_code=status) is True


def test_backpressure_status_set_matches_spec():
    # 429/500/503 from the brief, plus the gateway-class 502/504.
    assert {429, 500, 503}.issubset(BACKPRESSURE_STATUS_CODES)


@pytest.mark.parametrize("status", [200, 201, 204, 400, 401, 403, 404, 409, 422])
def test_deterministic_statuses_are_not_backpressure(status):
    assert classify_backpressure(status_code=status) is False


@pytest.mark.parametrize(
    "exc",
    [
        httpx.TimeoutException("slow"),
        httpx.ReadTimeout("slow"),
        httpx.ConnectTimeout("slow"),
        httpx.PoolTimeout("slow pool checkout"),  # connection-pool saturation
    ],
)
def test_timeouts_and_pool_checkout_are_backpressure(exc):
    assert classify_backpressure(exception=exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("refused"),  # not load-driven -> must not back off
        ValueError("bug"),
    ],
)
def test_non_load_exceptions_are_not_backpressure(exc):
    assert classify_backpressure(exception=exc) is False


def test_high_latency_is_backpressure():
    assert classify_backpressure(latency=5.0, slow_threshold=1.0) is True


def test_latency_within_threshold_is_not_backpressure():
    assert classify_backpressure(latency=0.5, slow_threshold=1.0) is False


def test_latency_without_threshold_is_ignored():
    assert classify_backpressure(latency=99.0, slow_threshold=None) is False


def test_no_signal_is_not_backpressure():
    assert classify_backpressure() is False


# ---------------------------------------------------------------------------
# _LatencyMonitor (EWMA)
# ---------------------------------------------------------------------------


def test_latency_monitor_has_no_threshold_during_warmup():
    monitor = _LatencyMonitor(warmup=3, slow_multiplier=3.0)
    monitor.observe(0.1)
    monitor.observe(0.1)
    assert monitor.slow_threshold() is None


def test_latency_monitor_threshold_after_warmup():
    monitor = _LatencyMonitor(warmup=3, slow_multiplier=3.0, alpha=0.5)
    monitor.observe(1.0)
    monitor.observe(1.0)
    monitor.observe(1.0)
    # EWMA settles at 1.0, threshold = multiplier * ewma.
    assert monitor.slow_threshold() == pytest.approx(3.0)

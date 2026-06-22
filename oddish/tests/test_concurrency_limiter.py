"""Unit tests for the pure adaptive-concurrency primitives.

``AdaptiveConcurrencyLimiter`` is a gradient2 in-flight limiter (Netflix/Envoy
"adaptive concurrency"): each clean latency sample nudges the in-flight ceiling
toward ``limit * gradient + queue_size`` where ``gradient`` compares a no-load
RTT floor against the current RTT, and a hard multiplicative decrease still
fires immediately on explicit backpressure (429/5xx/timeout/pool checkout).
``classify_backpressure`` decides what counts as backpressure, and
``_LatencyMonitor`` supplies the two RTT statistics the gradient needs (a short
EWMA ``rtt_actual`` and a windowed-minimum ``rtt_noload``). All of these are
pure (no network, no thread pools) and tested in isolation here.
"""

from __future__ import annotations

import math

import httpx
import pytest

from oddish.cli._concurrency import (
    BACKPRESSURE_STATUS_CODES,
    DEFAULT_BACKOFF_FACTOR,
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_MIN_CONCURRENCY,
    MIN_BACKOFF_FACTOR,
    RTT_TOLERANCE,
    AdaptiveConcurrencyLimiter,
    _LatencyMonitor,
    classify_backpressure,
    compute_gradient_limit,
    parse_submit_concurrency_header,
)


# ---------------------------------------------------------------------------
# compute_gradient_limit: the pure gradient2 law
# ---------------------------------------------------------------------------


def test_gradient_at_floor_holds_when_noload_equals_actual():
    # gradient = clamp(1.5 * 0.1/0.1, 0.5, 1.0) = 1.0; queue = ceil(sqrt(16)) = 4
    # new = 16*1.0 + 4 = 20; smoothed = 0.8*16 + 0.2*20 = 16.8 (grows toward target)
    out = compute_gradient_limit(16.0, rtt_noload=0.1, rtt_actual=0.1)
    assert out == pytest.approx(16.8)


def test_gradient_shrinks_when_actual_exceeds_noload():
    # gradient = clamp(1.5 * 0.1/0.3, .5, 1) = 0.5; queue = 4; new = 16*0.5 + 4 = 12
    # smoothed = 0.8*16 + 0.2*12 = 15.2
    out = compute_gradient_limit(16.0, rtt_noload=0.1, rtt_actual=0.3)
    assert out == pytest.approx(15.2)


def test_gradient_holds_at_unity_target():
    # gradient = 1.5*0.1/0.2 = 0.75; queue = 4; new = 16*0.75 + 4 = 16; smoothed = 16
    out = compute_gradient_limit(16.0, rtt_noload=0.1, rtt_actual=0.2)
    assert out == pytest.approx(16.0)


def test_gradient_lower_clamp_at_half():
    # huge actual -> raw gradient ~0 but clamped at 0.5; new = 16*0.5 + 4 = 12
    out = compute_gradient_limit(16.0, rtt_noload=0.1, rtt_actual=10.0)
    assert out == pytest.approx(15.2)


def test_gradient_upper_clamp_at_one():
    # noload >> actual -> raw gradient >1 but clamped at 1.0; new = 16 + 4 = 20
    out = compute_gradient_limit(16.0, rtt_noload=10.0, rtt_actual=0.1)
    assert out == pytest.approx(16.8)


def test_gradient_queue_size_is_sqrt_of_limit():
    # queue = ceil(sqrt(100)) = 10; gradient 1.0; new = 110; smoothed = 0.8*100+0.2*110=102
    out = compute_gradient_limit(100.0, rtt_noload=0.1, rtt_actual=0.1)
    assert out == pytest.approx(102.0)
    assert math.ceil(math.sqrt(100.0)) == 10


# ---------------------------------------------------------------------------
# AdaptiveConcurrencyLimiter: gradient growth + app-limited guard
# ---------------------------------------------------------------------------


def test_default_limiter_starts_at_floor():
    limiter = AdaptiveConcurrencyLimiter()
    assert limiter.min_limit == DEFAULT_MIN_CONCURRENCY == 4
    assert limiter.max_limit == DEFAULT_MAX_CONCURRENCY == 64
    assert limiter.limit == DEFAULT_MIN_CONCURRENCY


def test_clean_samples_grow_limit_toward_max():
    # A run of clean low-latency samples (noload == actual -> gradient 1.0) grows
    # the limit, with the client pushing the ceiling so the app-limited guard
    # allows growth.
    limiter = AdaptiveConcurrencyLimiter(min_limit=4, max_limit=64, initial_limit=8)
    for _ in range(50):
        limiter.record_sample(0.1, in_flight=64)
    assert limiter.limit > 8


def test_gradient_growth_clamps_at_max():
    limiter = AdaptiveConcurrencyLimiter(min_limit=4, max_limit=12, initial_limit=4)
    for _ in range(200):
        limiter.record_sample(0.1, in_flight=64)
    assert limiter.limit == 12


def test_app_limited_sample_does_not_grow():
    # in_flight * 2 < limit -> the client is not pushing the ceiling, so a clean
    # sample must not grow it (Netflix anti-creep).
    limiter = AdaptiveConcurrencyLimiter(min_limit=4, max_limit=64, initial_limit=20)
    before = limiter.limit
    for _ in range(20):
        limiter.record_sample(0.1, in_flight=1)
    assert limiter.limit == before


def test_rising_latency_shrinks_even_when_app_limited():
    # A shrink (degraded gradient) always applies, regardless of utilization.
    limiter = AdaptiveConcurrencyLimiter(min_limit=4, max_limit=64, initial_limit=40)
    limiter.record_sample(0.1, in_flight=40)  # seed the no-load floor
    for _ in range(40):
        limiter.record_sample(1.0, in_flight=1)  # actual >> noload -> shrink
    assert limiter.limit < 40


def test_sustained_load_keeps_gradient_below_one():
    # The no-load floor is a windowed MIN, not an average: after one low sample
    # seeds the floor, a long run of high-latency samples must keep rtt_noload
    # low (so the gradient stays < 1 and the limiter keeps shrinking). An EWMA
    # baseline would drift up to rtt_actual and blind the limiter here.
    limiter = AdaptiveConcurrencyLimiter(min_limit=4, max_limit=64, initial_limit=40)
    limiter.record_sample(0.1, in_flight=40)
    for _ in range(50):
        limiter.record_sample(0.5, in_flight=40)
    assert limiter.rtt_noload == pytest.approx(0.1)
    assert limiter.rtt_actual > limiter.rtt_noload
    gradient = RTT_TOLERANCE * limiter.rtt_noload / limiter.rtt_actual
    assert gradient < 1.0


# ---------------------------------------------------------------------------
# AdaptiveConcurrencyLimiter: advertised-max clamp (D1 header)
# ---------------------------------------------------------------------------


def test_advertised_max_clamps_below_static_max():
    limiter = AdaptiveConcurrencyLimiter(min_limit=4, max_limit=64, initial_limit=40)
    limiter.set_advertised_max(10)
    for _ in range(100):
        limiter.record_sample(0.1, in_flight=64)
    assert limiter.limit == 10  # min(static 64, advertised 10)


def test_advertised_max_reclamps_current_limit_immediately():
    limiter = AdaptiveConcurrencyLimiter(min_limit=4, max_limit=64, initial_limit=50)
    assert limiter.limit == 50
    limiter.set_advertised_max(12)
    assert limiter.limit == 12


def test_advertised_max_never_drops_below_floor():
    limiter = AdaptiveConcurrencyLimiter(min_limit=4, max_limit=64, initial_limit=20)
    limiter.set_advertised_max(1)  # below floor -> floor wins
    assert limiter.limit == 4


def test_advertised_max_cleared_restores_static_max():
    limiter = AdaptiveConcurrencyLimiter(min_limit=4, max_limit=64, initial_limit=40)
    limiter.set_advertised_max(10)
    assert limiter.limit == 10
    limiter.set_advertised_max(None)
    for _ in range(200):
        limiter.record_sample(0.1, in_flight=64)
    assert limiter.limit == 64


# ---------------------------------------------------------------------------
# AdaptiveConcurrencyLimiter: hard multiplicative decrease (kept)
# ---------------------------------------------------------------------------


def test_backpressure_multiplies_by_backoff_factor():
    assert DEFAULT_BACKOFF_FACTOR == pytest.approx(0.9)
    limiter = AdaptiveConcurrencyLimiter(min_limit=4, max_limit=64, initial_limit=16)
    assert limiter.record_backpressure() == 14  # 16 * 0.9 = 14.4 -> 14
    assert limiter.limit <= 15


def test_backpressure_clamps_at_floor():
    limiter = AdaptiveConcurrencyLimiter(min_limit=4, max_limit=64, initial_limit=5)
    for _ in range(20):
        limiter.record_backpressure()
    assert limiter.limit == 4


def test_backoff_factor_floored_at_half():
    limiter = AdaptiveConcurrencyLimiter(
        min_limit=1, max_limit=64, initial_limit=16, backoff_factor=0.1
    )
    assert limiter.backoff_factor == MIN_BACKOFF_FACTOR == 0.5
    assert limiter.record_backpressure() == 8  # 16 * 0.5, not 16 * 0.1


def test_clean_sample_and_backpressure_stay_within_band():
    limiter = AdaptiveConcurrencyLimiter(min_limit=4, max_limit=64, initial_limit=8)
    for _ in range(50):
        limiter.record_sample(0.1, in_flight=64)
        limiter.record_backpressure()
        assert 4 <= limiter.limit <= 64


# ---------------------------------------------------------------------------
# AdaptiveConcurrencyLimiter: pinned + validation
# ---------------------------------------------------------------------------


def test_pinned_limiter_never_moves():
    # min == max pins the limit: neither a gradient sample nor backpressure moves it.
    limiter = AdaptiveConcurrencyLimiter(min_limit=7, max_limit=7, initial_limit=7)
    assert limiter.limit == 7
    for _ in range(20):
        limiter.record_sample(0.1, in_flight=99)
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
# classify_backpressure (status + exception only; latency is the gradient's job)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", sorted(BACKPRESSURE_STATUS_CODES))
def test_transient_statuses_are_backpressure(status):
    assert classify_backpressure(status_code=status) is True


def test_backpressure_status_set_matches_spec():
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


def test_no_signal_is_not_backpressure():
    assert classify_backpressure() is False


# ---------------------------------------------------------------------------
# _LatencyMonitor: short EWMA (rtt_actual) + windowed minimum (rtt_noload)
# ---------------------------------------------------------------------------


def test_latency_monitor_empty_is_none():
    monitor = _LatencyMonitor()
    assert monitor.rtt_actual() is None
    assert monitor.noload_rtt() is None


def test_rtt_actual_is_ewma():
    monitor = _LatencyMonitor(alpha=0.2)
    monitor.observe(2.0)
    assert monitor.rtt_actual() == pytest.approx(2.0)
    # 0.2*4 + 0.8*2 = 2.4
    monitor.observe(4.0)
    assert monitor.rtt_actual() == pytest.approx(2.4)


def test_noload_rtt_is_windowed_minimum_not_average():
    monitor = _LatencyMonitor()
    for sample in (0.1, 0.5, 0.5, 0.5):
        monitor.observe(sample)
    assert monitor.noload_rtt() == pytest.approx(0.1)  # min, not the ~0.3 average
    assert monitor.rtt_actual() > monitor.noload_rtt()


def test_noload_window_reprobes_as_samples_expire():
    # A small window re-probes the floor: once the low sample ages out, the
    # no-load minimum rises to the prevailing latency.
    monitor = _LatencyMonitor(noload_window=3)
    monitor.observe(0.1)
    for _ in range(3):
        monitor.observe(0.5)  # evicts the 0.1
    assert monitor.noload_rtt() == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# parse_submit_concurrency_header (D1 advertised ceiling)
# ---------------------------------------------------------------------------


def test_parse_header_extracts_ceiling():
    assert parse_submit_concurrency_header("ceiling=48; pressure=0.25; ttl=5") == 48


def test_parse_header_tolerates_whitespace_and_order():
    assert parse_submit_concurrency_header("pressure=0.1;  ceiling=12 ; ttl=5") == 12


@pytest.mark.parametrize("value", [None, "", "pressure=0.5; ttl=5", "ceiling=NaN"])
def test_parse_header_returns_none_when_absent_or_garbled(value):
    assert parse_submit_concurrency_header(value) is None

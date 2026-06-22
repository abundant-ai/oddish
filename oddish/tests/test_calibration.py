"""Tests for the offline, read-only per-queue concurrency calibration.

The calibration aggregates historic ``trials`` per ``queue_key`` into the inputs
the dynamic-concurrency design needs: realized throughput, average trial
duration, average tokens per trial (``r_tokens``), the provider-throttle rate
(reusing the existing ``classify_retry_reason`` matcher), and token coverage. The
pure summarizer and the rate-limit counter are tested in isolation; the DB query
function is tested against a fake session so the suite needs no Postgres.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from oddish.core import calibration
from oddish.core.calibration import (
    QueueCalibration,
    count_rate_limit_by_queue,
    format_calibration_json,
    summarize_calibration,
)


# ---------------------------------------------------------------------------
# summarize_calibration (pure)
# ---------------------------------------------------------------------------


def _agg_row(**kw):
    base = {
        "queue_key": "openai/gpt-5.2",
        "provider": "openai",
        "trials": 100,
        "token_covered": 80,
        "finished": 90,
        "avg_trial_seconds": 120.0,
        "r_tokens": 5000.0,
    }
    base.update(kw)
    return base


def test_summarize_computes_metrics():
    rows = [_agg_row()]
    out = summarize_calibration(rows, {"openai/gpt-5.2": 5}, window_minutes=60)
    assert len(out) == 1
    c = out[0]
    assert c.queue_key == "openai/gpt-5.2"
    assert c.provider == "openai"
    assert c.trials == 100
    assert c.token_coverage == pytest.approx(0.8)  # 80/100
    assert c.avg_trial_seconds == pytest.approx(120.0)
    assert c.r_tokens == pytest.approx(5000.0)
    assert c.throughput_per_min == pytest.approx(1.5)  # 90 finished / 60 min
    assert c.throttle_rate == pytest.approx(0.05)  # 5 / 100


def test_summarize_skips_zero_trial_rows():
    out = summarize_calibration([_agg_row(trials=0)], {}, window_minutes=60)
    assert out == []


def test_summarize_passes_through_null_duration_and_tokens():
    rows = [
        _agg_row(avg_trial_seconds=None, r_tokens=None, token_covered=0, finished=0)
    ]
    c = summarize_calibration(rows, {}, window_minutes=60)[0]
    assert c.avg_trial_seconds is None
    assert c.r_tokens is None
    assert c.token_coverage == 0.0
    assert c.throughput_per_min == 0.0
    assert c.throttle_rate == 0.0


def test_summarize_zero_window_is_safe():
    c = summarize_calibration([_agg_row()], {}, window_minutes=0)[0]
    assert c.throughput_per_min == 0.0


# ---------------------------------------------------------------------------
# count_rate_limit_by_queue (reuses classify_retry_reason)
# ---------------------------------------------------------------------------


def test_count_rate_limit_reuses_matcher():
    rows = [
        ("openai/gpt-5.2", "Error: 429 Too Many Requests"),
        ("openai/gpt-5.2", "rate limit exceeded"),
        ("openai/gpt-5.2", "tokens per minute quota"),
        ("openai/gpt-5.2", "some unrelated harness crash"),
        ("anthropic/opus", "throttled by provider"),
        ("anthropic/opus", None),
    ]
    counts = count_rate_limit_by_queue(rows)
    assert counts == {"openai/gpt-5.2": 3, "anthropic/opus": 1}


def test_count_rate_limit_empty():
    assert count_rate_limit_by_queue([]) == {}


# ---------------------------------------------------------------------------
# calibrate_model_concurrency (DB query wiring, fake session)
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """Returns canned result sets in call order (agg query, then throttle query)."""

    def __init__(self, result_sets):
        self._result_sets = list(result_sets)
        self._i = 0
        self.queries = []

    async def execute(self, query):
        self.queries.append(query)
        rows = self._result_sets[self._i]
        self._i += 1
        return _FakeResult(rows)


@pytest.mark.asyncio
async def test_calibrate_model_concurrency_wires_queries():
    agg_rows = [
        SimpleNamespace(
            queue_key="openai/gpt-5.2",
            provider="openai",
            trials=100,
            token_covered=100,
            finished=100,
            avg_trial_seconds=120.0,
            r_tokens=5000.0,
        )
    ]
    throttle_rows = [
        SimpleNamespace(queue_key="openai/gpt-5.2", error_message="429 rate limit"),
        SimpleNamespace(queue_key="openai/gpt-5.2", error_message="boom"),
    ]
    session = _FakeSession([agg_rows, throttle_rows])

    out = await calibration.calibrate_model_concurrency(session, window_minutes=100)

    assert len(session.queries) == 2  # aggregate + throttle
    assert len(out) == 1
    c = out[0]
    assert c.queue_key == "openai/gpt-5.2"
    assert c.token_coverage == pytest.approx(1.0)
    assert c.throughput_per_min == pytest.approx(1.0)  # 100 finished / 100 min
    assert c.throttle_rate == pytest.approx(0.01)  # 1 rate-limit of 100 trials


# ---------------------------------------------------------------------------
# formatting + CLI entrypoint
# ---------------------------------------------------------------------------


def test_format_calibration_json_roundtrips():
    rows = [
        QueueCalibration(
            queue_key="openai/gpt-5.2",
            provider="openai",
            trials=100,
            token_coverage=0.8,
            avg_trial_seconds=120.0,
            r_tokens=5000.0,
            throughput_per_min=1.5,
            throttle_rate=0.05,
        )
    ]
    parsed = json.loads(format_calibration_json(rows))
    assert parsed == [
        {
            "queue_key": "openai/gpt-5.2",
            "provider": "openai",
            "trials": 100,
            "token_coverage": 0.8,
            "avg_trial_seconds": 120.0,
            "r_tokens": 5000.0,
            "throughput_per_min": 1.5,
            "throttle_rate": 0.05,
        }
    ]


def test_main_prints_json(monkeypatch, capsys):
    sample = [
        QueueCalibration(
            queue_key="openai/gpt-5.2",
            provider="openai",
            trials=10,
            token_coverage=1.0,
            avg_trial_seconds=60.0,
            r_tokens=1000.0,
            throughput_per_min=0.5,
            throttle_rate=0.0,
        )
    ]

    async def _fake_load(window_minutes):
        assert window_minutes == 4242
        return sample

    monkeypatch.setattr(calibration, "_load_calibration", _fake_load)
    rc = calibration.main(["--window-minutes", "4242"])
    assert rc == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed[0]["queue_key"] == "openai/gpt-5.2"

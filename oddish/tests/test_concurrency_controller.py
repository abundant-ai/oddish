"""Tests for the per-model concurrency controller.

The controller derives, per ``queue_key`` each reconcile cycle, an advisory
concurrency limit: a feed-forward provider-limit ceiling (:func:`compute_ceiling`)
trimmed by a queue-feedback AIMD loop (:func:`controller_step`) with latched
saturation cooldown and a sustained-demand counter. The pure law is tested here
(step response, hysteresis, anti-windup, never-above-ceiling); the DB wiring and
decay-to-static read are tested against a fake session.
"""

from __future__ import annotations

import pytest

from oddish.workers.queue import concurrency_controller as cc
from oddish.workers.queue.concurrency_controller import (
    BETA,
    GLOBAL_MAX,
    K_UP,
    N_SUSTAIN,
    compute_ceiling,
    controller_step,
)


# ---------------------------------------------------------------------------
# compute_ceiling -- feed-forward provider-limit ceiling
# ---------------------------------------------------------------------------


def test_ceiling_fallback_without_bucket_is_double_base():
    assert (
        compute_ceiling(base=8, bucket=None, trial_seconds=120, tokens_per_trial=5000)
        == 16
    )


def test_ceiling_fallback_clamped_to_global_max():
    assert (
        compute_ceiling(base=200, bucket=None, trial_seconds=120, tokens_per_trial=5000)
        == GLOBAL_MAX
    )


def test_ceiling_tpm_is_binding_constraint():
    # C_rpm = 10000*120/(60*1) = 20000; C_tpm = 400000*120/(60*5000) = 160;
    # min=160, *0.6 headroom = 96.
    bucket = {"rpm": 10000, "tpm": 400000, "headroom": 0.6}
    assert (
        compute_ceiling(
            base=8, bucket=bucket, trial_seconds=120, tokens_per_trial=5000, r_calls=1
        )
        == 96
    )


def test_ceiling_rpm_is_binding_constraint():
    # C_rpm = 50*120/(60*2) = 50; C_tpm huge; min=50, *0.6 = 30.
    bucket = {"rpm": 50, "tpm": 10_000_000, "headroom": 0.6}
    assert (
        compute_ceiling(
            base=8, bucket=bucket, trial_seconds=120, tokens_per_trial=100, r_calls=2
        )
        == 30
    )


def test_ceiling_low_token_coverage_uses_rpm_only():
    # tokens_per_trial unknown -> drop the TPM bound, lean on RPM.
    # C_rpm = 50*120/60 = 100, *0.6 = 60.
    bucket = {"rpm": 50, "tpm": 400000, "headroom": 0.6}
    assert (
        compute_ceiling(
            base=8, bucket=bucket, trial_seconds=120, tokens_per_trial=None, r_calls=1
        )
        == 60
    )


def test_ceiling_never_below_static_base():
    bucket = {"rpm": 1, "tpm": 1, "headroom": 0.6}
    assert (
        compute_ceiling(
            base=8, bucket=bucket, trial_seconds=1, tokens_per_trial=1, r_calls=1
        )
        == 8  # computed ceiling < base -> clamped up to base
    )


def test_ceiling_operator_override_is_absolute_cap():
    bucket = {"rpm": 10000, "tpm": 400000, "headroom": 0.6}
    # computed 96, override 20 -> 20; override may even pull below base.
    assert (
        compute_ceiling(
            base=8,
            bucket=bucket,
            trial_seconds=120,
            tokens_per_trial=5000,
            r_calls=1,
            override=20,
        )
        == 20
    )
    assert (
        compute_ceiling(
            base=8,
            bucket=bucket,
            trial_seconds=120,
            tokens_per_trial=5000,
            r_calls=1,
            override=4,
        )
        == 4
    )


def test_ceiling_bucket_without_trial_seconds_falls_back():
    bucket = {"rpm": 10000, "tpm": 400000}
    assert (
        compute_ceiling(
            base=8, bucket=bucket, trial_seconds=None, tokens_per_trial=5000
        )
        == 16  # no residence time -> 2x base fallback
    )


# ---------------------------------------------------------------------------
# controller_step -- queue-feedback AIMD with hysteresis
# ---------------------------------------------------------------------------


def _step(**kw):
    base = dict(
        prev_limit=10,
        cooldown=False,
        demand=0,
        fill=0.7,
        queued=0,
        wait_p95=0.0,
        throttle_rate=0.0,
        ceiling=100,
    )
    base.update(kw)
    return controller_step(**base)


def test_idle_queue_shrinks():
    d = _step(fill=0.1, queued=0, prev_limit=10)
    assert d.error == -1
    assert d.new_limit == 7  # floor(10 * 0.70)


def test_dead_band_holds_no_flap():
    d = _step(fill=0.7, queued=5, prev_limit=10)  # between U_LOW and U_HIGH
    assert d.error == 0
    assert d.new_limit == 10


def test_growth_requires_sustained_demand():
    # First bottleneck vote: demand 0 -> 1, not yet sustained -> hold.
    first = _step(fill=0.95, queued=5, prev_limit=10, demand=0)
    assert first.error == 1
    assert first.demand == 1
    assert first.new_limit == 10  # N_SUSTAIN not reached
    # Second consecutive vote: demand 1 -> 2 -> grow by K_UP.
    second = _step(fill=0.95, queued=5, prev_limit=10, demand=1)
    assert second.demand >= N_SUSTAIN
    assert second.new_limit == 10 + K_UP


def test_saturation_cuts_multiplicatively_and_latches_cooldown():
    d = _step(wait_p95=200.0, prev_limit=20)  # 200 > 120*1.5
    assert d.error == -1
    assert d.new_limit == int(20 * BETA)  # 14
    assert d.cooldown is True


def test_throttle_spike_forces_a_cut():
    d = _step(throttle_rate=0.10, fill=0.95, queued=5, prev_limit=20)
    assert d.error == -1
    assert d.cooldown is True
    assert d.new_limit < 20


def test_cooldown_latched_suppresses_growth_until_recovered():
    # Still elevated (between resume 90s and cut 180s): cooldown stays, no growth.
    d = _step(cooldown=True, wait_p95=100.0, fill=0.95, queued=5, prev_limit=10)
    assert d.cooldown is True
    assert d.error == 0
    assert d.new_limit == 10  # growth suppressed while latched


def test_cooldown_releases_below_resume_threshold():
    d = _step(cooldown=True, wait_p95=80.0, throttle_rate=0.0, fill=0.7, queued=0)
    assert d.cooldown is False  # 80 < 120*0.75 -> released


def test_demand_resets_on_non_grow_vote():
    d = _step(fill=0.1, queued=0, demand=5)  # idle -> e=-1 -> demand resets
    assert d.demand == 0


def test_grow_never_exceeds_ceiling():
    d = _step(fill=0.95, queued=5, prev_limit=10, demand=N_SUSTAIN, ceiling=11)
    assert d.new_limit == 11  # 10 + K_UP would be 12, clamped to ceiling


def test_oversubscribed_fill_drains():
    # fill > 1 (ceiling lowered below running): drain, never grow.
    d = _step(fill=1.5, queued=5, prev_limit=10)
    assert d.error == -1
    assert d.new_limit == 7


def test_limit_never_below_floor():
    d = _step(fill=0.1, queued=0, prev_limit=1)
    assert d.new_limit == 1  # floor(1 * 0.7) = 0 -> clamped to 1


def test_none_signals_are_safe():
    d = _step(fill=None, wait_p95=None, queued=0, prev_limit=10)
    assert d.new_limit >= 1  # None treated as 0.0, no crash


def test_module_exposes_stale_ttl_three_cycles():
    # decay-to-static guard: advisory is trusted only within 3x the 240s cadence.
    assert cc.STALE_TTL_SECONDS == 720


# ---------------------------------------------------------------------------
# recompute_advisory_limits / get_advisory_limits (DB wiring, fake session)
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """Fake AsyncSession: serves the advisory SELECT and captures upserts."""

    def __init__(self, state_rows=None, *, fail=False):
        self.state_rows = state_rows or []
        self.fail = fail
        self.upserts: list[dict] = []

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if self.fail:
            raise RuntimeError("db down")
        if "INSERT INTO model_concurrency_advisory" in sql:
            self.upserts.append(params)
            return _Result([])
        if "model_concurrency_advisory" in sql:  # a SELECT
            return _Result(self.state_rows)
        return _Result([])


def _health(capacity):
    from types import SimpleNamespace

    return SimpleNamespace(capacity=capacity)


def _cap(queue_key, *, running, queued, wait_p95):
    from types import SimpleNamespace

    return SimpleNamespace(
        queue_key=queue_key, running=running, queued=queued, wait_p95_seconds=wait_p95
    )


def _cal(
    queue_key,
    *,
    throttle_rate=0.0,
    avg_trial_seconds=120.0,
    r_tokens=5000.0,
    token_coverage=1.0,
):
    from oddish.core.calibration import QueueCalibration

    return QueueCalibration(
        queue_key=queue_key,
        provider="openai",
        trials=100,
        token_coverage=token_coverage,
        avg_trial_seconds=avg_trial_seconds,
        r_tokens=r_tokens,
        throughput_per_min=1.0,
        throttle_rate=throttle_rate,
    )


def _state_row(queue_key, advisory_limit, **state):
    from types import SimpleNamespace

    return SimpleNamespace(
        queue_key=queue_key, advisory_limit=advisory_limit, state=state
    )


@pytest.mark.asyncio
async def test_recompute_upserts_shrink_for_idle_queue(monkeypatch):
    monkeypatch.setattr(
        cc,
        "get_queue_health_core",
        _make_async(
            _health([_cap("openai/gpt-5.2", running=0, queued=0, wait_p95=0.0)])
        ),
    )
    monkeypatch.setattr(
        cc, "calibrate_model_concurrency", _make_async([_cal("openai/gpt-5.2")])
    )
    recorded = []
    monkeypatch.setattr(
        cc,
        "record_queue_runtime_status",
        _make_async(None, record=recorded),
    )
    # Defaults: no override -> base = default_model_concurrency (8); no provider
    # bucket -> ceiling = min(base*2, GLOBAL_MAX) = 16; no running -> idle -> shrink.
    session = _FakeSession(state_rows=[])  # no prior advisory -> prev_limit = base 8
    updated = await cc.recompute_advisory_limits(session)

    assert updated == {"openai/gpt-5.2": 5}  # floor(8 * 0.70)
    assert len(session.upserts) == 1
    assert session.upserts[0]["queue_key"] == "openai/gpt-5.2"
    assert session.upserts[0]["advisory_limit"] == 5
    assert recorded and recorded[0][0] == cc.CONCURRENCY_CONTROLLER_COMPONENT


@pytest.mark.asyncio
async def test_recompute_fill_is_relative_to_advisory_not_static(monkeypatch):
    # Advisory already trimmed to 4 (below static 8) and fully utilized (running 4)
    # with a backlog: utilization must read as full (4/4), NOT idle (4/8), so the
    # controller does not wrongly shrink it.
    monkeypatch.setattr(
        cc,
        "get_queue_health_core",
        _make_async(
            _health([_cap("openai/gpt-5.2", running=4, queued=10, wait_p95=0.0)])
        ),
    )
    monkeypatch.setattr(
        cc, "calibrate_model_concurrency", _make_async([_cal("openai/gpt-5.2")])
    )
    monkeypatch.setattr(cc, "record_queue_runtime_status", _make_async(None))
    session = _FakeSession(state_rows=[_state_row("openai/gpt-5.2", 4, demand=0)])
    updated = await cc.recompute_advisory_limits(session)
    # full advisory + backlog -> bottleneck vote (held this cycle, NOT cut to 2).
    assert updated == {"openai/gpt-5.2": 4}


@pytest.mark.asyncio
async def test_recompute_low_token_coverage_drops_tpm_bound(monkeypatch):
    captured = {}

    def fake_ceiling(**kwargs):
        captured.update(kwargs)
        return 16

    monkeypatch.setattr(cc, "compute_ceiling", fake_ceiling)
    monkeypatch.setattr(
        cc,
        "get_queue_health_core",
        _make_async(
            _health([_cap("openai/gpt-5.2", running=0, queued=0, wait_p95=0.0)])
        ),
    )
    monkeypatch.setattr(
        cc,
        "calibrate_model_concurrency",
        _make_async([_cal("openai/gpt-5.2", token_coverage=0.2)]),  # sparse tokens
    )
    monkeypatch.setattr(cc, "record_queue_runtime_status", _make_async(None))
    session = _FakeSession(state_rows=[])
    await cc.recompute_advisory_limits(session)
    assert captured["tokens_per_trial"] is None  # dropped: lean on RPM


@pytest.mark.asyncio
async def test_recompute_skips_statically_disabled_queue(monkeypatch):
    # A statically-disabled queue (get_model_concurrency == 0) must not get an
    # advisory above zero -- the operator's off switch is honored.
    class _StubSettings:
        model_concurrency_overrides: dict = {}

        def get_model_concurrency(self, queue_key):
            return 0  # disabled

        def normalize_queue_key(self, queue_key):
            return queue_key

        def get_provider_rate_limit(self, queue_key):
            return None

    monkeypatch.setattr(cc, "settings", _StubSettings())
    monkeypatch.setattr(
        cc,
        "get_queue_health_core",
        _make_async(
            _health([_cap("disabled/model", running=0, queued=0, wait_p95=0.0)])
        ),
    )
    monkeypatch.setattr(
        cc, "calibrate_model_concurrency", _make_async([_cal("disabled/model")])
    )
    monkeypatch.setattr(cc, "record_queue_runtime_status", _make_async(None))
    session = _FakeSession(state_rows=[])
    updated = await cc.recompute_advisory_limits(session)
    assert updated == {}  # disabled queue skipped
    assert session.upserts == []


@pytest.mark.asyncio
async def test_recompute_is_best_effort_on_error(monkeypatch):
    monkeypatch.setattr(
        cc,
        "get_queue_health_core",
        _make_async(_health([_cap("q", running=0, queued=0, wait_p95=0.0)])),
    )
    monkeypatch.setattr(cc, "calibrate_model_concurrency", _make_async([_cal("q")]))
    session = _FakeSession(fail=True)  # every execute raises
    assert await cc.recompute_advisory_limits(session) == {}  # swallowed, no raise


@pytest.mark.asyncio
async def test_get_advisory_limits_returns_fresh_clamped():
    from types import SimpleNamespace

    rows = [
        SimpleNamespace(queue_key="openai/gpt-5.2", advisory_limit=40),
        SimpleNamespace(queue_key="anthropic/opus", advisory_limit=9999),  # clamp
    ]
    session = _FakeSession(state_rows=rows)
    out = await cc.get_advisory_limits(session)
    assert out == {"openai/gpt-5.2": 40, "anthropic/opus": cc.GLOBAL_MAX}


@pytest.mark.asyncio
async def test_get_advisory_limits_tolerates_missing_table():
    session = _FakeSession(fail=True)
    assert await cc.get_advisory_limits(session) == {}


def _make_async(value, *, record=None):
    async def _fn(*args, **kwargs):
        if record is not None:
            record.append(args)
        return value

    return _fn


# ---------------------------------------------------------------------------
# merge_advisory_over_static -- the dispatcher's effective-limit overlay
# ---------------------------------------------------------------------------


def test_merge_advisory_overrides_only_active_static_keys():
    static = {"openai/gpt-5.2": 8, "anthropic/opus": 8}
    advisory = {"openai/gpt-5.2": 40, "google/gemini-3": 99}
    merged = cc.merge_advisory_over_static(static, advisory)
    # advisory wins for an active queue; an advisory-only key (not currently
    # active) is ignored; an un-advised active queue keeps its static value.
    assert merged == {"openai/gpt-5.2": 40, "anthropic/opus": 8}


def test_merge_never_overrides_a_statically_disabled_queue():
    # A queue disabled via static limit 0 stays disabled even if a stale positive
    # advisory row exists (the operator's off switch wins immediately).
    static = {"openai/gpt-5.2": 0}
    advisory = {"openai/gpt-5.2": 40}
    assert cc.merge_advisory_over_static(static, advisory) == {"openai/gpt-5.2": 0}


def test_merge_empty_advisory_is_static():
    static = {"openai/gpt-5.2": 8}
    assert cc.merge_advisory_over_static(static, {}) == {"openai/gpt-5.2": 8}


@pytest.mark.asyncio
async def test_enabled_dispatcher_path_uses_advisory(monkeypatch):
    # End-to-end of the ENABLED dispatcher read: a fresh advisory limit overrides
    # the static one for an active queue.
    from types import SimpleNamespace

    queue_keys = ["openai/gpt-5.2", "anthropic/opus"]
    static = {qk: 8 for qk in queue_keys}
    session = _FakeSession(
        state_rows=[SimpleNamespace(queue_key="openai/gpt-5.2", advisory_limit=40)]
    )
    advisory = await cc.get_advisory_limits(session)
    effective = cc.merge_advisory_over_static(static, advisory)
    assert effective == {"openai/gpt-5.2": 40, "anthropic/opus": 8}

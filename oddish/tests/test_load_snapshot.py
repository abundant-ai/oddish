import pytest

from oddish.core import admin
from oddish.core.admin import QueueCapacityStat, QueueHealthResponse


def test_compute_submit_ceiling_idle_is_max():
    assert admin.compute_submit_ceiling(0.0) == admin.CLIENT_CEILING_MAX


def test_compute_submit_ceiling_saturated_is_floor():
    assert admin.compute_submit_ceiling(1.0) == admin.CLIENT_FLOOR


def test_compute_submit_ceiling_midpoint():
    # round(64 * (1 - 0.5)) == 32
    assert admin.compute_submit_ceiling(0.5) == 32


def test_compute_pressure_takes_max_term_and_clamps_to_one():
    # queue term 600/500 = 1.2 -> clamp to 1.0
    assert (
        admin.compute_pressure(
            wait_p95_max=0.0, totals_queued=600, sweep_rtt_p95_ewma=0.0
        )
        == 1.0
    )


def test_compute_pressure_none_inputs_are_zero():
    assert (
        admin.compute_pressure(
            wait_p95_max=None, totals_queued=0, sweep_rtt_p95_ewma=None
        )
        == 0.0
    )


def test_compute_pressure_rtt_term():
    # rtt 1.0 / budget 2.0 = 0.5 dominates
    p = admin.compute_pressure(
        wait_p95_max=0.0, totals_queued=0, sweep_rtt_p95_ewma=1.0
    )
    assert abs(p - 0.5) < 1e-9


@pytest.mark.asyncio
async def test_build_load_snapshot_maps_health(monkeypatch):
    health = QueueHealthResponse(
        totals_queued=600,
        totals_running=10,
        throughput=[],
        capacity=[
            QueueCapacityStat(
                queue_key="openai/gpt-5.2",
                queued=600,
                queued_scheduled=0,
                running=10,
                limit=48,
                fill=10 / 48,
                oldest_queued_age_seconds=12.0,
                wait_p50_seconds=3.0,
                wait_p95_seconds=30.0,
            )
        ],
        dispatcher=None,
        reconciler=None,
        timestamp="2026-06-22T00:00:00",
    )

    async def fake_health(session):
        return health

    async def fake_statuses(session):
        return {
            admin.SUBMIT_LATENCY_COMPONENT: {"payload": {"sweep_rtt_p95_ewma": 0.0}}
        }

    monkeypatch.setattr(admin, "get_queue_health_core", fake_health)
    monkeypatch.setattr(
        "oddish.workers.queue.runtime_status.get_queue_runtime_statuses", fake_statuses
    )

    snap = await admin.build_load_snapshot(session=None)

    assert snap.totals.queued == 600 and snap.totals.running == 10
    assert snap.pressure == 1.0  # queue term 600/500 clamps to 1.0
    assert snap.submit_ceiling == admin.CLIENT_FLOOR
    assert snap.ttl_seconds == int(admin.LOAD_CACHE_TTL_SECONDS)
    assert snap.timestamp == "2026-06-22T00:00:00"
    assert len(snap.queues) == 1
    q = snap.queues[0]
    assert q.queue_key == "openai/gpt-5.2"
    assert q.queued == 600 and q.running == 10 and q.limit == 48
    assert q.wait_p95_seconds == 30.0
    assert q.advisory_limit == 48 and q.limit_source == "static"

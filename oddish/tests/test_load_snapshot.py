from oddish.core import admin


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

from datetime import datetime, timezone

import pytest

from oddish.compare_token_costs import (
    TokenRates,
    estimate_token_cost,
    parse_utc_datetime,
)


def test_parse_utc_datetime_accepts_date_and_offset() -> None:
    assert parse_utc_datetime("2026-07-01") == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert parse_utc_datetime("2026-07-01T03:00:00-07:00") == datetime(
        2026, 7, 1, 10, tzinfo=timezone.utc
    )
    assert parse_utc_datetime("2026-07-01T00:00:00Z") == datetime(
        2026, 7, 1, tzinfo=timezone.utc
    )


def test_parse_utc_datetime_rejects_empty_value() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        parse_utc_datetime(" ")


def test_estimate_token_cost_prices_each_token_class() -> None:
    estimate = estimate_token_cost(
        uncached_input_tokens=1_000_000,
        cache_tokens=2_000_000,
        cache_write_tokens=3_000_000,
        output_tokens=4_000_000,
        rates=TokenRates(
            input=0.30,
            cache_read=0.06,
            cache_write=0.30,
            output=1.20,
        ),
    )

    assert estimate.uncached_input_usd == pytest.approx(0.30)
    assert estimate.cache_read_usd == pytest.approx(0.12)
    assert estimate.cache_write_usd == pytest.approx(0.90)
    assert estimate.output_usd == pytest.approx(4.80)
    assert estimate.total_usd == pytest.approx(6.12)


def test_estimate_token_cost_clamps_negative_counts() -> None:
    estimate = estimate_token_cost(
        uncached_input_tokens=-1,
        cache_tokens=-1,
        cache_write_tokens=-1,
        output_tokens=-1,
        rates=TokenRates(
            input=0.30,
            cache_read=0.06,
            cache_write=0.30,
            output=1.20,
        ),
    )

    assert estimate.total_usd == 0

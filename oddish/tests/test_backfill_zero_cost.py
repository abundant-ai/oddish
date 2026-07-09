from oddish.backfill_zero_cost import _plan_update


def test_priceable_row_writes_the_estimate() -> None:
    # A fake $0 and a NULL both get overwritten with the token estimate.
    assert _plan_update(0.0, 0.42) == ("cost", 0.42)
    assert _plan_update(None, 0.42) == ("cost", 0.42)


def test_unpriceable_zero_flips_to_null() -> None:
    # $0 is not authoritative for an unpriceable model: settle it to NULL so
    # quota bills the flat unpriced rate instead of treating the $0 as real.
    assert _plan_update(0.0, None) == ("cost", None)


def test_unpriceable_null_is_skipped() -> None:
    # Already NULL and still unpriceable -> no write (avoids NULL -> NULL churn).
    assert _plan_update(None, None) is None

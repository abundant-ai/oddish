"""Unit tests for the shared settled-cost reducer (no DB required).

The SQL column builder's cancelled/unpriced gating is exercised by the DB-backed
quota tests (``test_quotas_usage`` / ``test_org_quotas_usage``); here we pin the
pure-Python reduction: native passthrough, token estimate, and the $0 floor.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import settings  # noqa: E402
from oddish.core.cost_basis import (  # noqa: E402
    settled_cost_from_row,
    settled_cost_parts,
    sum_settled_cost,
)
from oddish.model_pricing import estimate_cost_usd  # noqa: E402


def _row(
    *,
    model=None,
    native_cost=0.0,
    est_input=0,
    est_output=0,
    est_cache=0,
    est_trials=0,
):
    return SimpleNamespace(
        model=model,
        native_cost=native_cost,
        est_input=est_input,
        est_output=est_output,
        est_cache=est_cache,
        est_trials=est_trials,
    )


def test_native_cost_passes_through_untouched():
    native, estimated = settled_cost_parts(_row(native_cost=1.5))
    assert native == 1.5
    assert estimated == 0.0


def test_unpriced_with_tokens_is_token_estimated():
    model = "gpt-5.5-pro"
    expected = estimate_cost_usd(model, 1_000_000, 500_000, 0)
    assert expected and expected > 0
    native, estimated = settled_cost_parts(
        _row(model=model, est_input=1_000_000, est_output=500_000, est_trials=1)
    )
    assert native == 0.0
    assert estimated == expected


def test_unpriced_without_tokens_defaults_to_zero_floor():
    # No pricing/tokens -> estimate None -> floor (default $0).
    _, estimated = settled_cost_parts(_row(model=None, est_trials=5))
    assert estimated == 0.0


def test_floor_re_enables_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "unpriced_trial_cost_usd", Decimal("1.00"))
    _, estimated = settled_cost_parts(_row(model=None, est_trials=3))
    assert estimated == 3.0


def test_sum_and_from_row_combine_native_plus_estimate():
    model = "gpt-5.5-pro"
    est = estimate_cost_usd(model, 1_000_000, 500_000, 0)
    rows = [
        _row(native_cost=0.25),
        _row(model=model, est_input=1_000_000, est_output=500_000, est_trials=1),
    ]
    assert settled_cost_from_row(rows[0]) == 0.25
    assert sum_settled_cost(rows) == 0.25 + est

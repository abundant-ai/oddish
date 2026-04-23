"""Tests for the static model pricing / cost estimation table."""

from __future__ import annotations

import pytest

from oddish.model_pricing import (
    PRICING_TABLE,
    ModelPricing,
    estimate_cost_usd,
    has_pricing,
)


def test_pricing_table_is_ordered_from_specific_to_general() -> None:
    """Earlier patterns must never be a substring of a later pattern.

    If ``gpt-5`` appeared before ``gpt-5-mini`` the substring match would
    shadow the mini entry and we'd price mini calls at the full GPT-5 rate.
    """
    patterns = [p for p, _ in PRICING_TABLE]
    for i, earlier in enumerate(patterns):
        for later in patterns[i + 1 :]:
            assert earlier not in later, (
                f"Pricing pattern {earlier!r} (pos {i}) is a substring of "
                f"later pattern {later!r}; move {later!r} above {earlier!r}."
            )


def test_has_pricing_handles_varied_model_strings() -> None:
    assert has_pricing("anthropic/claude-sonnet-4-5-20250929")
    assert has_pricing("bedrock/global.anthropic.claude-opus-4-5")
    assert has_pricing("openai/gpt-5-codex")
    assert has_pricing("openai/gpt-5.1-codex-mini")
    assert has_pricing("gemini-2.5-flash-lite")
    assert not has_pricing(None)
    assert not has_pricing("")
    assert not has_pricing("totally-made-up-model")


def test_estimate_cost_combines_input_output_cache_correctly() -> None:
    # Sonnet 4.5: in=3e-6, out=15e-6, cache_read=3e-7
    # 1000 input (no cache), 500 output
    value = estimate_cost_usd("claude-sonnet-4-5", 1000, 500, None)
    assert value == pytest.approx(1000 * 3e-6 + 500 * 15e-6)

    # With cache: 1000 input (600 cached), 500 output.
    value = estimate_cost_usd("claude-sonnet-4-5-20250929", 1000, 500, 600)
    expected = 400 * 3e-6 + 600 * 3e-7 + 500 * 15e-6
    assert value == pytest.approx(expected)


def test_estimate_cost_returns_none_for_unknown_model() -> None:
    assert estimate_cost_usd("some-unknown-model", 100, 100, 0) is None
    assert estimate_cost_usd(None, 100, 100, 0) is None


def test_estimate_cost_returns_none_when_no_tokens() -> None:
    assert estimate_cost_usd("gpt-5", 0, 0, 0) is None
    assert estimate_cost_usd("gpt-5", None, None, None) is None


def test_mini_variants_match_mini_pricing_not_base() -> None:
    """Substring-order regression: gpt-5-mini must not resolve to gpt-5."""
    mini = estimate_cost_usd("openai/gpt-5-mini", 1_000, 1_000, 0)
    base = estimate_cost_usd("openai/gpt-5", 1_000, 1_000, 0)
    assert mini is not None and base is not None
    assert mini < base, "gpt-5-mini should be cheaper than gpt-5"


def test_pricing_dataclass_is_immutable() -> None:
    p = ModelPricing(input=1e-6, output=2e-6)
    with pytest.raises(Exception):  # frozen dataclass -> FrozenInstanceError
        p.input = 3e-6  # type: ignore[misc]

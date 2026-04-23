"""Tests for model pricing / cost estimation.

These tests cover both the primary path (``litellm.model_cost``) and the
hand-maintained ``PRICING_TABLE`` fallback.  The fallback tests exercise
``_find_local_pricing`` directly so they stay deterministic even as
litellm's table drifts.
"""

from __future__ import annotations

import pytest

from oddish.model_pricing import (
    PRICING_TABLE,
    ModelPricing,
    _find_litellm_pricing,
    _find_local_pricing,
    _litellm_candidates,
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


def test_has_pricing_covers_latest_frontier_models() -> None:
    """2026-era models released after the initial pricing table shipped."""
    # Anthropic — Opus 4.6 / 4.7 and Sonnet 4.6 share the 4.5 rate card.
    assert has_pricing("claude-opus-4-7")
    assert has_pricing("claude-opus-4-7-20260416")
    assert has_pricing("anthropic/claude-opus-4-7")
    assert has_pricing("bedrock/global.anthropic.claude-opus-4-7")
    assert has_pricing("claude-opus-4-6")
    assert has_pricing("claude-sonnet-4-6-20260112")
    # OpenAI — GPT-5.5 family and the 5.2 / 5.3 / 5.4 variants.
    assert has_pricing("openai/gpt-5.5")
    assert has_pricing("gpt-5.5-pro")
    assert has_pricing("gpt-5.5-codex")
    assert has_pricing("openai/gpt-5.4")
    assert has_pricing("gpt-5.4-mini")
    assert has_pricing("gpt-5.4-nano")
    assert has_pricing("gpt-5.4-pro")
    assert has_pricing("gpt-5.3")
    assert has_pricing("gpt-5.3-codex")
    assert has_pricing("gpt-5.2")
    assert has_pricing("gpt-5.2-codex")
    assert has_pricing("gpt-5.2-pro")
    # Google — Gemini 3.1.
    assert has_pricing("gemini-3.1-pro")
    assert has_pricing("gemini-3.1-flash-lite")


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


def test_pro_and_codex_suffixes_do_not_fall_back_to_base() -> None:
    """Regression: gpt-5.5-pro / gpt-5.5-codex must not resolve to gpt-5.5."""
    base = estimate_cost_usd("openai/gpt-5.5", 1_000, 1_000, 0)
    pro = estimate_cost_usd("openai/gpt-5.5-pro", 1_000, 1_000, 0)
    codex = estimate_cost_usd("openai/gpt-5.5-codex", 1_000, 1_000, 0)
    assert base is not None and pro is not None and codex is not None
    # Pro is ~6x more expensive than base 5.5; codex matches 5.5.
    assert pro > base
    assert codex == pytest.approx(base)


def test_opus_4x_rate_card_is_consistent() -> None:
    """Opus 4.5 / 4.6 / 4.7 all bill at $5 / $25 / $0.50 per M tokens."""
    costs = [
        estimate_cost_usd(f"anthropic/claude-opus-{suffix}", 1_000, 1_000, 500)
        for suffix in ("4-5", "4-6", "4-7")
    ]
    assert all(c is not None for c in costs)
    assert costs[0] == pytest.approx(costs[1]) == pytest.approx(costs[2])


def test_gpt_5_5_estimated_cost_matches_published_rate() -> None:
    """GPT-5.5 is $5 / $30 per M tokens ($5e-6 / $3e-5); cache read $0.50/M."""
    # 1M uncached input + 1M output + 500K cached input.
    value = estimate_cost_usd("gpt-5.5-20260423", 1_500_000, 1_000_000, 500_000)
    expected = 1_000_000 * 5e-6 + 500_000 * 5e-7 + 1_000_000 * 30e-6
    assert value == pytest.approx(expected)


def test_codex_mini_does_not_shadow_gpt_5_codex_mini() -> None:
    """``codex-mini`` is a suffix of ``gpt-5.1-codex-mini``; ordering must
    route gpt-5.1-codex-mini to its own (cheaper) rate card."""
    v51 = estimate_cost_usd("openai/gpt-5.1-codex-mini", 1_000, 1_000, 0)
    v_codex = estimate_cost_usd("openai/codex-mini-latest", 1_000, 1_000, 0)
    assert v51 is not None and v_codex is not None
    assert v51 < v_codex


def test_pricing_dataclass_is_immutable() -> None:
    p = ModelPricing(input=1e-6, output=2e-6)
    with pytest.raises(Exception):  # frozen dataclass -> FrozenInstanceError
        p.input = 3e-6  # type: ignore[misc]


# ---------------------------------------------------------------------------
# LiteLLM primary path
# ---------------------------------------------------------------------------


def test_litellm_resolves_known_frontier_models() -> None:
    """LiteLLM ships pricing for ~2,700 models; common ones must resolve."""
    assert _find_litellm_pricing("claude-opus-4-7") is not None
    assert _find_litellm_pricing("claude-sonnet-4-6") is not None
    assert _find_litellm_pricing("gpt-5-mini") is not None


def test_litellm_handles_provider_prefix_and_dated_suffix() -> None:
    """``openai/gpt-5.4`` and ``claude-opus-4-7-20260416`` must both resolve."""
    assert _find_litellm_pricing("openai/gpt-5.4") is not None
    assert _find_litellm_pricing("anthropic/claude-opus-4-7-20260416") is not None
    assert _find_litellm_pricing("bedrock/global.anthropic.claude-opus-4-7") is not None


def test_litellm_candidates_normalize_names() -> None:
    """Candidate generator should strip prefixes, dotted namespaces, dates."""
    cs = _litellm_candidates("bedrock/global.anthropic.claude-opus-4-7-20260416")
    # We don't care about order, just that the useful forms appear.
    assert "global.anthropic.claude-opus-4-7-20260416" in cs
    assert "claude-opus-4-7-20260416" in cs
    assert "claude-opus-4-7" in cs
    assert "anthropic/claude-opus-4-7" in cs


def test_litellm_returns_none_for_unknown_model() -> None:
    assert _find_litellm_pricing("totally-made-up-model-xyz") is None


# ---------------------------------------------------------------------------
# Local fallback path (independent of litellm)
# ---------------------------------------------------------------------------


def test_local_fallback_covers_every_pattern_in_table() -> None:
    """Every entry in PRICING_TABLE must be resolvable by the local lookup."""
    for pattern, expected in PRICING_TABLE:
        got = _find_local_pricing(pattern)
        assert got is expected, f"Local lookup failed for {pattern!r}"


def test_local_fallback_prices_brand_new_models_litellm_doesnt_cover() -> None:
    """The gap table exists to cover models too new for the installed litellm.

    gpt-5.5-pro was announced the same day oddish shipped this test and
    isn't in any litellm release yet.  Same for bare-name Gemini 3 variants
    (litellm only has ``gemini-3-pro-preview``).
    """
    pro = _find_local_pricing("openai/gpt-5.5-pro")
    assert pro is not None
    assert pro.input == pytest.approx(30e-6)
    assert pro.output == pytest.approx(180e-6)

    gemini = _find_local_pricing("gemini-3.1-pro")
    assert gemini is not None
    assert gemini.input == pytest.approx(2e-6)
    assert gemini.output == pytest.approx(12e-6)

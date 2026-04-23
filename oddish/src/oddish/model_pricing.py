"""Model pricing table for estimating trial cost from token counts.

Harbor does not populate ``cost_usd`` on AgentContext for every provider, so
we fall back to a static pricing table to estimate cost from the captured
input / cache / output token counts. Prices are per-token (not per-million)
and sourced from LiteLLM's ``model_prices_and_context_window.json`` reference.

Model-name matching uses substring patterns because trajectory / agent data
uses varying formats (e.g. ``claude-sonnet-4-5-20250929``,
``anthropic/claude-opus-4-5``, ``bedrock/global.anthropic.claude-sonnet-4-5``,
``gpt-5.1-codex``). More-specific patterns MUST appear before less-specific
ones to avoid false substring matches (e.g. ``gpt-5-mini`` before ``gpt-5``).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPricing:
    """Per-token pricing for a single model family."""

    input: float
    output: float
    cache_read: float | None = None


PRICING_TABLE: list[tuple[str, ModelPricing]] = [
    # Anthropic — Claude 4.x family. Opus 4.5 dropped in price vs 4 / 4.1.
    ("claude-opus-4-5", ModelPricing(input=5e-6, output=25e-6, cache_read=5e-7)),
    ("claude-opus-4-1", ModelPricing(input=15e-6, output=75e-6, cache_read=1.5e-6)),
    ("claude-opus-4", ModelPricing(input=15e-6, output=75e-6, cache_read=1.5e-6)),
    ("claude-sonnet-4-5", ModelPricing(input=3e-6, output=15e-6, cache_read=3e-7)),
    ("claude-sonnet-4", ModelPricing(input=3e-6, output=15e-6, cache_read=3e-7)),
    ("claude-haiku-4-5", ModelPricing(input=1e-6, output=5e-6, cache_read=1e-7)),
    ("claude-haiku-4", ModelPricing(input=1e-6, output=5e-6, cache_read=1e-7)),
    # Anthropic — older Claude 3.x still showing up in legacy runs.
    ("claude-3-7-sonnet", ModelPricing(input=3e-6, output=15e-6, cache_read=3e-7)),
    ("claude-3-5-sonnet", ModelPricing(input=3e-6, output=15e-6, cache_read=3e-7)),
    ("claude-3-5-haiku", ModelPricing(input=8e-7, output=4e-6, cache_read=8e-8)),
    ("claude-3.5-sonnet", ModelPricing(input=3e-6, output=15e-6, cache_read=3e-7)),
    ("claude-3.5-haiku", ModelPricing(input=8e-7, output=4e-6, cache_read=8e-8)),
    ("claude-3-opus", ModelPricing(input=15e-6, output=75e-6, cache_read=1.5e-6)),
    # Google — Gemini 3.x
    ("gemini-3-pro", ModelPricing(input=2e-6, output=12e-6, cache_read=2e-7)),
    ("gemini-3-flash", ModelPricing(input=5e-7, output=3e-6, cache_read=5e-8)),
    # Google — Gemini 2.5
    ("gemini-2.5-flash-lite", ModelPricing(input=1e-7, output=4e-7, cache_read=1e-8)),
    ("gemini-2.5-flash", ModelPricing(input=3e-7, output=2.5e-6, cache_read=3e-8)),
    ("gemini-2.5-pro", ModelPricing(input=1.25e-6, output=10e-6, cache_read=1.25e-7)),
    # OpenAI — GPT-5.x
    ("gpt-5.1-codex-mini", ModelPricing(input=2.5e-7, output=2e-6, cache_read=2.5e-8)),
    ("gpt-5.1-codex", ModelPricing(input=1.25e-6, output=10e-6, cache_read=1.25e-7)),
    ("gpt-5.1", ModelPricing(input=1.25e-6, output=10e-6, cache_read=1.25e-7)),
    ("gpt-5-codex", ModelPricing(input=1.25e-6, output=10e-6, cache_read=1.25e-7)),
    ("gpt-5-mini", ModelPricing(input=2.5e-7, output=2e-6, cache_read=2.5e-8)),
    ("gpt-5-nano", ModelPricing(input=5e-8, output=4e-7, cache_read=5e-9)),
    ("gpt-5-pro", ModelPricing(input=15e-6, output=120e-6)),
    ("gpt-5", ModelPricing(input=1.25e-6, output=10e-6, cache_read=1.25e-7)),
    # OpenAI — GPT-4.1 / 4o.
    ("gpt-4.1-mini", ModelPricing(input=4e-7, output=1.6e-6, cache_read=1e-7)),
    ("gpt-4.1-nano", ModelPricing(input=1e-7, output=4e-7, cache_read=2.5e-8)),
    ("gpt-4.1", ModelPricing(input=2e-6, output=8e-6, cache_read=5e-7)),
    ("gpt-4o-mini", ModelPricing(input=1.5e-7, output=6e-7, cache_read=7.5e-8)),
    ("gpt-4o", ModelPricing(input=2.5e-6, output=10e-6, cache_read=1.25e-6)),
    # OpenAI — reasoning.
    ("o4-mini", ModelPricing(input=1.1e-6, output=4.4e-6, cache_read=2.75e-7)),
    ("o3-pro", ModelPricing(input=20e-6, output=80e-6)),
    ("o3-mini", ModelPricing(input=1.1e-6, output=4.4e-6, cache_read=5.5e-7)),
    ("o3", ModelPricing(input=2e-6, output=8e-6, cache_read=5e-7)),
    # OpenAI — standalone codex endpoint.
    ("codex-mini", ModelPricing(input=1.5e-6, output=6e-6, cache_read=3.75e-7)),
]


def _find_pricing(model_name: str) -> ModelPricing | None:
    lower = model_name.lower()
    for pattern, pricing in PRICING_TABLE:
        if pattern in lower:
            return pricing
    return None


def has_pricing(model_name: str | None) -> bool:
    """Return True iff the pricing table has an entry for this model."""
    if not model_name:
        return False
    return _find_pricing(model_name) is not None


def estimate_cost_usd(
    model_name: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    cached_tokens: int | None = None,
) -> float | None:
    """Estimate USD cost from token counts and a model name.

    ``cached_tokens`` is the subset of ``input_tokens`` that were served from
    prompt cache; those tokens are billed at ``cache_read`` instead of the
    full input rate. Returns ``None`` when the model is not in the pricing
    table or there are no tokens to price.
    """
    if not model_name:
        return None
    input_total = int(input_tokens or 0)
    output_total = int(output_tokens or 0)
    if input_total == 0 and output_total == 0:
        return None
    pricing = _find_pricing(model_name)
    if pricing is None:
        return None

    cached = int(cached_tokens or 0)
    uncached_input = max(0, input_total - cached)
    cache_rate = pricing.cache_read if pricing.cache_read is not None else pricing.input

    return (
        uncached_input * pricing.input
        + cached * cache_rate
        + output_total * pricing.output
    )

"""Model pricing table for estimating cost from token counts.

Ported from sauron's model-pricing.ts. Prices are per-token (not per-million).
Source: LiteLLM model_prices_and_context_window.json

We match model names using substring patterns since trajectory data uses
varying formats (e.g. "claude-sonnet-4-5-20250929", "gemini-3-flash-preview").
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPricing:
    input: float  # cost per input token
    output: float  # cost per output token
    cache_read: float | None = None  # cost per cached-read token
    cache_write: float | None = None  # cost per cache-creation token (defaults to input * 1.25)


# Ordered so more-specific patterns match first.
# More-specific patterns (e.g. "gpt-5.4") MUST appear before less-specific
# ones (e.g. "gpt-5") to avoid false substring matches.
PRICING_TABLE: list[tuple[str, ModelPricing]] = [
    # Anthropic — Opus 4.5/4.6 got a price drop from original Opus 4
    ("claude-opus-4-6", ModelPricing(input=5e-6, output=25e-6, cache_read=5e-7)),
    ("claude-opus-4-5", ModelPricing(input=5e-6, output=25e-6, cache_read=5e-7)),
    ("claude-opus-4-1", ModelPricing(input=15e-6, output=75e-6, cache_read=1.5e-6)),
    ("claude-opus-4", ModelPricing(input=15e-6, output=75e-6, cache_read=1.5e-6)),
    # Anthropic — Sonnet
    ("claude-sonnet-4", ModelPricing(input=3e-6, output=15e-6, cache_read=3e-7)),
    # Anthropic — Haiku
    ("claude-haiku-4", ModelPricing(input=1e-6, output=5e-6, cache_read=1e-7)),
    ("claude-3-5-haiku", ModelPricing(input=8e-7, output=4e-6, cache_read=8e-8)),
    ("claude-3.5-haiku", ModelPricing(input=8e-7, output=4e-6, cache_read=8e-8)),
    # Google — Gemini 3.1
    ("gemini-3.1-flash-lite", ModelPricing(input=2.5e-7, output=1.5e-6, cache_read=2.5e-8)),
    ("gemini-3.1-pro", ModelPricing(input=2e-6, output=12e-6, cache_read=2e-7)),
    # Google — Gemini 3
    ("gemini-3-pro", ModelPricing(input=2e-6, output=12e-6, cache_read=2e-7)),
    ("gemini-3-flash", ModelPricing(input=5e-7, output=3e-6, cache_read=5e-8)),
    # Google — Gemini 2.5
    ("gemini-2.5-pro", ModelPricing(input=1.25e-6, output=10e-6, cache_read=1.25e-7)),
    ("gemini-2.5-flash-lite", ModelPricing(input=1e-7, output=4e-7, cache_read=1e-8)),
    ("gemini-2.5-flash", ModelPricing(input=3e-7, output=2.5e-6, cache_read=3e-8)),
    # OpenAI — GPT-5.4
    ("gpt-5.4-mini", ModelPricing(input=7.5e-7, output=4.5e-6, cache_read=7.5e-8)),
    ("gpt-5.4-nano", ModelPricing(input=2e-7, output=1.25e-6, cache_read=2e-8)),
    ("gpt-5.4-pro", ModelPricing(input=30e-6, output=180e-6, cache_read=3e-6)),
    ("gpt-5.4", ModelPricing(input=2.5e-6, output=15e-6, cache_read=2.5e-7)),
    # OpenAI — GPT-5.x
    ("gpt-5.3-codex", ModelPricing(input=1.75e-6, output=14e-6, cache_read=1.75e-7)),
    ("gpt-5.3", ModelPricing(input=1.75e-6, output=14e-6, cache_read=1.75e-7)),
    ("gpt-5.2-codex", ModelPricing(input=1.75e-6, output=14e-6, cache_read=1.75e-7)),
    ("gpt-5.2-pro", ModelPricing(input=21e-6, output=168e-6)),
    ("gpt-5.2", ModelPricing(input=1.75e-6, output=14e-6, cache_read=1.75e-7)),
    ("gpt-5.1-codex-mini", ModelPricing(input=2.5e-7, output=2e-6, cache_read=2.5e-8)),
    ("gpt-5.1-codex", ModelPricing(input=1.25e-6, output=10e-6, cache_read=1.25e-7)),
    ("gpt-5.1", ModelPricing(input=1.25e-6, output=10e-6, cache_read=1.25e-7)),
    # OpenAI — GPT-5 base
    ("gpt-5-codex", ModelPricing(input=1.25e-6, output=10e-6, cache_read=1.25e-7)),
    ("gpt-5-mini", ModelPricing(input=2.5e-7, output=2e-6, cache_read=2.5e-8)),
    ("gpt-5-nano", ModelPricing(input=5e-8, output=4e-7, cache_read=5e-9)),
    ("gpt-5-pro", ModelPricing(input=15e-6, output=120e-6)),
    ("gpt-5", ModelPricing(input=1.25e-6, output=10e-6, cache_read=1.25e-7)),
    # OpenAI — Codex
    ("codex-mini", ModelPricing(input=1.5e-6, output=6e-6, cache_read=3.75e-7)),
    # OpenAI — GPT-4.1
    ("gpt-4.1-mini", ModelPricing(input=4e-7, output=1.6e-6, cache_read=1e-7)),
    ("gpt-4.1-nano", ModelPricing(input=1e-7, output=4e-7, cache_read=2.5e-8)),
    ("gpt-4.1", ModelPricing(input=2e-6, output=8e-6, cache_read=5e-7)),
    # OpenAI — GPT-4o
    ("gpt-4o-mini", ModelPricing(input=1.5e-7, output=6e-7, cache_read=7.5e-8)),
    ("gpt-4o", ModelPricing(input=2.5e-6, output=10e-6, cache_read=1.25e-6)),
    # OpenAI — reasoning
    ("o4-mini", ModelPricing(input=1.1e-6, output=4.4e-6, cache_read=2.75e-7)),
    ("o3-pro", ModelPricing(input=20e-6, output=80e-6)),
    ("o3-mini", ModelPricing(input=1.1e-6, output=4.4e-6, cache_read=5.5e-7)),
    ("o3", ModelPricing(input=2e-6, output=8e-6, cache_read=5e-7)),
]


def _find_pricing(model_name: str) -> ModelPricing | None:
    lower = model_name.lower()
    for pattern, pricing in PRICING_TABLE:
        if pattern in lower:
            return pricing
    return None


def estimate_cost_usd(
    model_name: str | None,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int | None = None,
) -> float | None:
    """Estimate cost in USD from token counts and a model name.

    cached_tokens is a subset of input_tokens — they're charged at a discounted
    rate instead of the full input rate.

    Returns None if the model is not in the pricing table or there are no tokens.
    """
    if not model_name:
        return None
    if input_tokens == 0 and output_tokens == 0:
        return None
    pricing = _find_pricing(model_name)
    if not pricing:
        return None

    cached = cached_tokens or 0
    uncached_input = max(0, input_tokens - cached)
    cache_read_cost = pricing.cache_read if pricing.cache_read is not None else pricing.input

    return (
        uncached_input * pricing.input
        + cached * cache_read_cost
        + output_tokens * pricing.output
    )

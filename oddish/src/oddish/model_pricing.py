"""Token pricing: LiteLLM first, local gap table second."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any


@dataclass(frozen=True)
class ModelPricing:
    input: float
    output: float
    cache_read: float | None = None
    cache_write: float | None = None


# Gap table for models LiteLLM does not resolve under names we store.
# Verified gaps on 2026-04-23 against litellm>=1.80.8:
#   * Gemini 3 / 3.1 bare names (litellm only has ``*-preview`` variants).
#   * gpt-5.5-pro and gpt-5.5-codex (model launched today).
#   * gpt-5.3 bare (litellm has gpt-5.3-codex only).
#   * Legacy Claude 3.5 with dotted notation (litellm uses dashed form).
#   * claude-haiku-4 bare (litellm has claude-haiku-4-5 only).
# Ordering invariant: earlier patterns must not be substrings of later ones.
PRICING_TABLE: list[tuple[str, ModelPricing]] = [
    # Anthropic legacy / bare variants.
    ("claude-haiku-4", ModelPricing(input=1e-6, output=5e-6, cache_read=1e-7, cache_write=1.25e-6)),
    ("claude-3-7-sonnet", ModelPricing(input=3e-6, output=15e-6, cache_read=3e-7, cache_write=3.75e-6)),
    ("claude-3.5-sonnet", ModelPricing(input=3e-6, output=15e-6, cache_read=3e-7, cache_write=3.75e-6)),
    ("claude-3.5-haiku", ModelPricing(input=8e-7, output=4e-6, cache_read=8e-8, cache_write=1e-6)),
    # Google bare Gemini 3.x names.
    (
        "gemini-3.1-flash-lite",
        ModelPricing(input=2.5e-7, output=1.5e-6, cache_read=2.5e-8),
    ),
    ("gemini-3.1-pro", ModelPricing(input=2e-6, output=12e-6, cache_read=2e-7)),
    ("gemini-3-pro", ModelPricing(input=2e-6, output=12e-6, cache_read=2e-7)),
    ("gemini-3-flash", ModelPricing(input=5e-7, output=3e-6, cache_read=5e-8)),
    # OpenAI GPT-5.5 family.
    ("gpt-5.5-codex", ModelPricing(input=5e-6, output=30e-6, cache_read=5e-7)),
    ("gpt-5.5-pro", ModelPricing(input=30e-6, output=180e-6)),
    # OpenAI bare gpt-5.3.
    ("gpt-5.3", ModelPricing(input=1.75e-6, output=14e-6, cache_read=1.75e-7)),
    # Cursor Composer. Rates are published $/1M tokens / 1e6.
    # Sources (cursor.com / TokenCost / Vantage, 2026-06):
    #   Composer 2.5 (standard): $0.50 in / $2.50 out / $0.20 cache-read
    #   Composer 2   (fast):     $1.50 in / $7.50 out / $0.35 cache-read
    #   Composer 1.5:            $3.50 in / $17.50 out / $0.35 cache-read
    # Versioned patterns must precede bare "composer".
    (
        "composer-2-fast",
        ModelPricing(input=1.5e-6, output=7.5e-6, cache_read=3.5e-7),
    ),
    ("composer-2.5", ModelPricing(input=5e-7, output=2.5e-6, cache_read=2e-7)),
    (
        "composer-1.5",
        ModelPricing(input=3.5e-6, output=17.5e-6, cache_read=3.5e-7),
    ),
    ("composer-2", ModelPricing(input=5e-7, output=2.5e-6, cache_read=2e-7)),
    ("composer", ModelPricing(input=5e-7, output=2.5e-6, cache_read=2e-7)),
]

_DATED_SUFFIX_RE = re.compile(r"-(20\d{6}|\d{4}-\d{2}-\d{2})$")
_VERSIONED_SUFFIX_RE = re.compile(r"-v\d+(?::\d+)?$")

_LITELLM_PREFIX_CANDIDATES: tuple[str, ...] = (
    "",
    "openai/",
    "anthropic/",
    "gemini/",
    "vertex_ai/",
    "bedrock/",
    "azure/",
)


def _litellm_candidates(model_name: str) -> list[str]:
    candidates: list[str] = []

    def add(c: str) -> None:
        if c and c not in candidates:
            candidates.append(c)

    add(model_name)

    if "/" in model_name:
        add(model_name.split("/", 1)[1])

    tail = model_name.rsplit("/", 1)[-1]
    if "." in tail:
        add(tail.rsplit(".", 1)[-1])

    for base in list(candidates):
        without_date = _DATED_SUFFIX_RE.sub("", base)
        if without_date != base:
            add(without_date)
        without_version = _VERSIONED_SUFFIX_RE.sub("", base)
        if without_version != base:
            add(without_version)

    normalized_bases = list(candidates)
    for base in normalized_bases:
        for prefix in _LITELLM_PREFIX_CANDIDATES:
            if prefix:
                add(f"{prefix}{base}")

    return candidates


def _pricing_from_litellm_info(info: dict[str, Any]) -> ModelPricing | None:
    input_cost = info.get("input_cost_per_token")
    output_cost = info.get("output_cost_per_token")
    if input_cost is None or output_cost is None:
        return None
    cache_read_cost = info.get("cache_read_input_token_cost")
    cache_write_cost = info.get("cache_creation_input_token_cost")
    return ModelPricing(
        input=float(input_cost),
        output=float(output_cost),
        cache_read=float(cache_read_cost) if cache_read_cost is not None else None,
        cache_write=float(cache_write_cost) if cache_write_cost is not None else None,
    )


@lru_cache(maxsize=1024)
def _find_litellm_pricing(model_name: str) -> ModelPricing | None:
    try:
        import litellm
    except ImportError:
        return None

    model_cost: dict[str, dict[str, Any]] = getattr(litellm, "model_cost", {})
    if not isinstance(model_cost, dict) or not model_cost:
        return None

    for candidate in _litellm_candidates(model_name):
        info = model_cost.get(candidate)
        if info is None:
            continue
        pricing = _pricing_from_litellm_info(info)
        if pricing is not None:
            return pricing
    return None


def _find_local_pricing(model_name: str) -> ModelPricing | None:
    lower = model_name.lower()
    for pattern, pricing in PRICING_TABLE:
        if pattern in lower:
            return pricing
    return None


def _find_pricing(model_name: str) -> ModelPricing | None:
    return _find_litellm_pricing(model_name) or _find_local_pricing(model_name)


def has_pricing(model_name: str | None) -> bool:
    return bool(model_name and _find_pricing(model_name) is not None)


def estimate_cost_usd(
    model_name: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    cached_tokens: int | None = None,
    cache_write_tokens: int | None = None,
) -> float | None:
    if not model_name:
        return None
    input_total = int(input_tokens or 0)
    output_total = int(output_tokens or 0)
    cache_write = int(cache_write_tokens or 0)
    if not (input_total or output_total or cache_write):
        return None
    pricing = _find_pricing(model_name)
    if pricing is None:
        return None

    cached = int(cached_tokens or 0)
    uncached_input = max(0, input_total - cached - cache_write)
    cache_read_rate = pricing.cache_read if pricing.cache_read is not None else pricing.input
    cache_write_rate = pricing.cache_write if pricing.cache_write is not None else pricing.input * 1.25

    return (
        uncached_input * pricing.input
        + cached * cache_read_rate
        + cache_write * cache_write_rate
        + output_total * pricing.output
    )

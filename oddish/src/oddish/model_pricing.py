"""Model pricing for estimating trial cost from token counts.

Harbor does not populate ``cost_usd`` on ``AgentContext`` for every provider:
agents that wrap CLI tools (gemini-cli, copilot-cli, some codex modes) only
parse whatever the CLI itself prints, and several CLIs report tokens but not
cost.  We backfill those trials here.

Pricing sources, in order of preference:

1. ``litellm.model_cost`` — the live pricing table shipped with every
   litellm release (~2,700 models; covers frontier Anthropic, OpenAI,
   Gemini, Bedrock, Vertex, Azure variants; auto-updates on ``uv lock``).
2. ``PRICING_TABLE`` below — a tiny gap table for models litellm's current
   release doesn't cover (brand-new launches, bare-name Gemini variants,
   some legacy Claude 3.x dotted names).  Entries become redundant when
   litellm catches up and should be deleted then.

Both paths normalize names: the litellm path tries a handful of common
prefix/suffix permutations (``openai/``, ``anthropic/``, ``gemini/``,
bedrock ``*.anthropic.*`` namespaces, dated suffixes like
``-20260416``).  The local path uses case-insensitive substring matching,
with the ordering invariant that earlier entries must not be a substring
of later ones (enforced by ``test_model_pricing.py``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any


@dataclass(frozen=True)
class ModelPricing:
    """Per-token pricing for a single model family."""

    input: float
    output: float
    cache_read: float | None = None


# ---------------------------------------------------------------------------
# Gap table — entries here are models litellm's current release does NOT
# cover (or covers only under a different name than we see in the wild).
#
# When litellm ships support for a model below, its entry here becomes
# redundant and can be deleted.  Keep this list small: the invariant is
# "every entry is something litellm genuinely doesn't resolve today."
#
# Verified gaps on 2026-04-23 against litellm>=1.80.8:
#   * Gemini 3 / 3.1 bare names (litellm only has ``*-preview`` variants).
#   * gpt-5.5-pro and gpt-5.5-codex (model launched today).
#   * gpt-5.3 bare (litellm has gpt-5.3-codex only).
#   * Legacy Claude 3.5 with dotted notation (litellm uses dashed form).
#   * claude-haiku-4 bare (litellm has claude-haiku-4-5 only).
# ---------------------------------------------------------------------------
PRICING_TABLE: list[tuple[str, ModelPricing]] = [
    # Anthropic legacy / bare variants not in litellm.
    ("claude-haiku-4", ModelPricing(input=1e-6, output=5e-6, cache_read=1e-7)),
    ("claude-3-7-sonnet", ModelPricing(input=3e-6, output=15e-6, cache_read=3e-7)),
    ("claude-3.5-sonnet", ModelPricing(input=3e-6, output=15e-6, cache_read=3e-7)),
    ("claude-3.5-haiku", ModelPricing(input=8e-7, output=4e-6, cache_read=8e-8)),
    # Google — bare Gemini 3.x names.  Litellm only registers these under
    # the ``gemini-3-pro-preview`` / ``vertex_ai/*`` keys; harbor trajectory
    # data often records the short form.
    (
        "gemini-3.1-flash-lite",
        ModelPricing(input=2.5e-7, output=1.5e-6, cache_read=2.5e-8),
    ),
    ("gemini-3.1-pro", ModelPricing(input=2e-6, output=12e-6, cache_read=2e-7)),
    ("gemini-3-pro", ModelPricing(input=2e-6, output=12e-6, cache_read=2e-7)),
    ("gemini-3-flash", ModelPricing(input=5e-7, output=3e-6, cache_read=5e-8)),
    # OpenAI — GPT-5.5 family launched 2026-04-23; not yet in litellm.
    # Headline pricing: $5 / $30 / $0.50 cache for base; pro $30 / $180.
    ("gpt-5.5-codex", ModelPricing(input=5e-6, output=30e-6, cache_read=5e-7)),
    ("gpt-5.5-pro", ModelPricing(input=30e-6, output=180e-6)),
    # OpenAI — bare gpt-5.3 (litellm has gpt-5.3-codex only; gpt-5.3-codex
    # is a substring of this pattern so it must come AFTER gpt-5.3-codex —
    # but since litellm resolves gpt-5.3-codex first, we never reach here
    # for that model.  Still, keep gpt-5.3 short to avoid shadowing.)
    ("gpt-5.3", ModelPricing(input=1.75e-6, output=14e-6, cache_read=1.75e-7)),
]


# ---------------------------------------------------------------------------
# Name normalization helpers
# ---------------------------------------------------------------------------

# e.g. "-20260416", "-2026-04-16".  Anthropic uses the compact form in model
# ids; OpenAI uses the dashed form.  Both are attached to the end of an
# otherwise-plain model name.
_DATED_SUFFIX_RE = re.compile(r"-(20\d{6}|\d{4}-\d{2}-\d{2})$")
# Anthropic/Bedrock versioned suffixes: "-v1", "-v1:0", "-v2".
_VERSIONED_SUFFIX_RE = re.compile(r"-v\d+(?::\d+)?$")

# Prefixes to try when the raw model name isn't found in litellm.model_cost.
# Order matters: try the most common providers first.  Empty string means
# "no prefix" (i.e. bare model name).
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
    """Return candidate keys for ``litellm.model_cost`` lookup.

    Litellm's table keys are inconsistent: some providers use bare names
    (``claude-opus-4-7``), some require a prefix (``openai/gpt-5.4``), and
    Bedrock/Vertex entries use dotted namespaces
    (``global.anthropic.claude-opus-4-7``).  We generate a short ordered
    list of candidates rather than trying to predict the "right" one.
    """
    candidates: list[str] = []

    def add(c: str) -> None:
        if c and c not in candidates:
            candidates.append(c)

    add(model_name)

    # Strip provider prefix ("openai/gpt-5" -> "gpt-5").
    if "/" in model_name:
        add(model_name.split("/", 1)[1])

    # Strip dotted namespace ("global.anthropic.claude-opus-4-7"
    # -> "claude-opus-4-7").  Keep the bedrock-style key intact above too.
    tail = model_name.rsplit("/", 1)[-1]
    if "." in tail:
        add(tail.rsplit(".", 1)[-1])

    # Strip dated/versioned suffixes from all candidates seen so far.
    for base in list(candidates):
        without_date = _DATED_SUFFIX_RE.sub("", base)
        if without_date != base:
            add(without_date)
        without_version = _VERSIONED_SUFFIX_RE.sub("", base)
        if without_version != base:
            add(without_version)

    # Try each normalized base with every common provider prefix.
    normalized_bases = list(candidates)
    for base in normalized_bases:
        for prefix in _LITELLM_PREFIX_CANDIDATES:
            if prefix:
                add(f"{prefix}{base}")

    return candidates


def _pricing_from_litellm_info(info: dict[str, Any]) -> ModelPricing | None:
    """Convert a litellm ``model_cost`` entry to a ``ModelPricing``."""
    input_cost = info.get("input_cost_per_token")
    output_cost = info.get("output_cost_per_token")
    if input_cost is None or output_cost is None:
        return None
    cache_cost = info.get("cache_read_input_token_cost")
    return ModelPricing(
        input=float(input_cost),
        output=float(output_cost),
        cache_read=float(cache_cost) if cache_cost is not None else None,
    )


@lru_cache(maxsize=1024)
def _find_litellm_pricing(model_name: str) -> ModelPricing | None:
    """Look up pricing in ``litellm.model_cost`` with name normalization.

    Cached because the same model is typically priced thousands of times
    in a single run (one per trial / trajectory turn in dashboard rollups).
    Returns ``None`` if litellm isn't installed or the model isn't in its
    pricing table.
    """
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
    """Substring match against the hand-maintained ``PRICING_TABLE``."""
    lower = model_name.lower()
    for pattern, pricing in PRICING_TABLE:
        if pattern in lower:
            return pricing
    return None


def _find_pricing(model_name: str) -> ModelPricing | None:
    """Resolve pricing for ``model_name``.

    Tries LiteLLM's live pricing first (auto-updates with each release),
    then falls back to the hand-maintained table for models too new for
    the installed litellm version.
    """
    pricing = _find_litellm_pricing(model_name)
    if pricing is not None:
        return pricing
    return _find_local_pricing(model_name)


def has_pricing(model_name: str | None) -> bool:
    """Return True iff we can price this model (litellm or local table)."""
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
    full input rate.  Returns ``None`` when the model is not in either
    pricing source or there are no tokens to price.
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

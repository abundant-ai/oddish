"""Token pricing: LiteLLM first, local gap table second."""

from __future__ import annotations

import math
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
#   * glm-5.2 (litellm's zai/ catalog stops at zai/glm-5.1; our stored id is
#     zai/glm-5.2). z.ai list price: docs.z.ai/guides/overview/pricing (2026-07)
#     = $1.4/M in, $4.4/M out, $0.26/M cached input.
#   * kimi-k2.7-code direct Moonshot route (litellm has the Fireworks listing,
#     but no ``moonshot/`` entry). Kimi API Platform list price:
#     platform.kimi.ai/docs/pricing/chat-k27-code (2026-07)
#     = $0.95/M in, $4/M out, $0.19/M cached input.
# Ordering invariant: earlier patterns must not be substrings of later ones.
PRICING_TABLE: list[tuple[str, ModelPricing]] = [
    ("glm-x-preview", ModelPricing(input=1e-6, output=3.2e-6, cache_read=2e-7)),
    ("glm-5.2", ModelPricing(input=1.4e-6, output=4.4e-6, cache_read=2.6e-7)),
    (
        "moonshot/kimi-k2.7-code",
        ModelPricing(input=9.5e-7, output=4e-6, cache_read=1.9e-7),
    ),
    ("glm-4.5-flash", ModelPricing(input=0.0, output=0.0)),
    ("glm-4.7-flash", ModelPricing(input=0.0, output=0.0)),
    # Anthropic legacy / bare variants.
    (
        "claude-haiku-4",
        ModelPricing(input=1e-6, output=5e-6, cache_read=1e-7, cache_write=1.25e-6),
    ),
    (
        "claude-3-7-sonnet",
        ModelPricing(input=3e-6, output=15e-6, cache_read=3e-7, cache_write=3.75e-6),
    ),
    (
        "claude-3.5-sonnet",
        ModelPricing(input=3e-6, output=15e-6, cache_read=3e-7, cache_write=3.75e-6),
    ),
    (
        "claude-3.5-haiku",
        ModelPricing(input=8e-7, output=4e-6, cache_read=8e-8, cache_write=1e-6),
    ),
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

_PROVIDER_ALIASES: dict[str, str] = {
    "fireworks": "fireworks_ai",
    "fw": "fireworks_ai",
    "moonshotai": "moonshot",
    "kimi": "moonshot",
    "z-ai": "zai",
    "z.ai": "zai",
}
_CLAUDE_DOTTED_VERSION_RE = re.compile(r"(claude-(?:opus|sonnet|haiku)-\d+)\.(\d+)")
_ANTHROPIC_DOTTED_NAMESPACE_RE = re.compile(
    r"^(?:(?:global|us|eu|au)\.)?anthropic\.(.+)$"
)

# Claude Code computes ``total_cost_usd`` from its own Anthropic model table.
# That value is authoritative for Anthropic/Bedrock Claude, but not when the
# same harness is pointed at an Anthropic-compatible third-party endpoint.
_CLAUDE_CODE_PASSTHROUGH_PROVIDERS: frozenset[str] = frozenset(
    {"fireworks", "fireworks_ai", "zai", "minimax", "moonshot", "openrouter"}
)


def untrusted_native_cost_providers(*, agent: str | None) -> frozenset[str]:
    """Providers whose native cost cannot be trusted for this agent."""
    normalized_agent = (agent or "").strip().lower()
    if "claude-code" not in normalized_agent:
        return frozenset()
    return _CLAUDE_CODE_PASSTHROUGH_PROVIDERS


def is_native_cost_trusted(*, agent: str | None, provider: str | None) -> bool:
    """Whether a harness-reported native cost is authoritative.

    Provider alone is insufficient: a LiteLLM-backed agent can report a valid
    native Fireworks cost. Only Claude Code's third-party compatibility routes
    use an Anthropic-only client-side price table for a non-Anthropic model.
    """
    normalized_provider = (provider or "").strip().lower()
    return normalized_provider not in untrusted_native_cost_providers(agent=agent)


def _spelling_variants(value: str) -> list[str]:
    """Return deterministic spelling variants from most to least specific.

    Provider/path handling lives in :func:`_litellm_candidates`; this helper
    only normalizes spellings of one candidate. The queue makes transforms
    compose (for example dotted Claude version + dated suffix) without adding
    one special case for every combination.
    """
    variants: list[str] = []
    pending = [value]

    while pending:
        candidate = pending.pop(0)
        if not candidate or candidate in variants:
            continue
        variants.append(candidate)

        without_date = _DATED_SUFFIX_RE.sub("", candidate)
        if without_date != candidate:
            pending.append(without_date)

        without_version = _VERSIONED_SUFFIX_RE.sub("", candidate)
        if without_version != candidate:
            pending.append(without_version)

        dashed_claude = _CLAUDE_DOTTED_VERSION_RE.sub(r"\1-\2", candidate)
        if dashed_claude != candidate:
            pending.append(dashed_claude)

        # Bedrock inference profiles use dotted namespaces such as
        # ``global.anthropic.claude-opus-4-8``. Strip only recognized
        # namespaces; never split arbitrary dots (``claude-opus-4.8`` is a
        # version spelling, not a namespace).
        match = _ANTHROPIC_DOTTED_NAMESPACE_RE.match(candidate)
        if match:
            pending.append(match.group(1))

    return variants


def _litellm_candidates(model_name: str) -> list[str]:
    """Generate price-table keys from exact identity to safe fallbacks.

    Model ids are paths: ``router/vendor/model``. Walk every suffix so a new
    OpenRouter model can fall back to LiteLLM's vendor or bare-model key before
    LiteLLM adds the router-specific entry. Exact/router-specific keys stay
    first because different providers can charge different prices for the same
    open-weight model.
    """
    candidates: list[str] = []

    def add_with_variants(value: str) -> None:
        for candidate in _spelling_variants(value):
            if candidate not in candidates:
                candidates.append(candidate)

    parts = [part for part in model_name.split("/") if part]
    for start in range(len(parts)):
        suffix_parts = parts[start:]
        suffix = "/".join(suffix_parts)
        add_with_variants(suffix)

        if len(suffix_parts) < 2:
            continue
        provider = suffix_parts[0].casefold()
        bare = "/".join(suffix_parts[1:])

        if provider in {"fireworks", "fw"}:
            # Oddish stores Fireworks short ids while LiteLLM's authoritative
            # key also has the full Fireworks model path.
            if bare.casefold().startswith("accounts/fireworks/"):
                add_with_variants(f"fireworks_ai/{bare}")
            elif "/" not in bare:
                add_with_variants(f"fireworks_ai/accounts/fireworks/models/{bare}")

        mapped_provider = _PROVIDER_ALIASES.get(provider)
        if mapped_provider:
            add_with_variants(f"{mapped_provider}/{bare}")

    # Bare model ids sometimes need a LiteLLM provider namespace. Add these
    # only after exact and provider-attributed candidates so a reseller's
    # distinct rate can never be silently replaced by a generic vendor rate.
    bare_candidates = [candidate for candidate in candidates if "/" not in candidate]
    for base in bare_candidates:
        for prefix in _LITELLM_PREFIX_CANDIDATES:
            if prefix:
                add_with_variants(f"{prefix}{base}")

    return candidates


def _pricing_from_litellm_info(info: dict[str, Any]) -> ModelPricing | None:
    input_cost = info.get("input_cost_per_token")
    output_cost = info.get("output_cost_per_token")
    if input_cost is None or output_cost is None:
        return None
    if not float(input_cost) and not float(output_cost):
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

    # Oddish canonical ids are lowercase, while LiteLLM contains mixed-case
    # keys (for example ``minimax/MiniMax-M3``). The catalog currently has no
    # casefold collision with different prices, so a case-insensitive index is
    # safe and removes per-model casing patches.
    model_cost_casefolded = {key.casefold(): info for key, info in model_cost.items()}
    for candidate in _litellm_candidates(model_name):
        info = model_cost_casefolded.get(candidate.casefold())
        if info is None:
            continue
        pricing = _pricing_from_litellm_info(info)
        if pricing is not None:
            return pricing
    return None


def _find_local_pricing(model_name: str) -> ModelPricing | None:
    for candidate in _litellm_candidates(model_name):
        lower = candidate.lower()
        for pattern, pricing in PRICING_TABLE:
            if pattern in lower:
                return pricing
    return None


def _find_pricing(model_name: str) -> ModelPricing | None:
    return _find_litellm_pricing(model_name) or _find_local_pricing(model_name)


def get_model_pricing(model_name: str | None) -> ModelPricing | None:
    if not model_name:
        return None
    return _find_pricing(model_name)


def has_pricing(model_name: str | None) -> bool:
    return get_model_pricing(model_name) is not None


def estimate_cost_usd(
    model_name: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    cached_tokens: int | None = None,
    cache_write_tokens: int | None = None,
) -> float | None:
    if not model_name:
        return None
    input_total = max(0, int(input_tokens or 0))
    output_total = max(0, int(output_tokens or 0))
    cache_write = max(0, int(cache_write_tokens or 0))
    if not (input_total or output_total or cache_write):
        return None
    pricing = get_model_pricing(model_name)
    if pricing is None:
        return None

    cached = max(0, int(cached_tokens or 0))
    uncached_input = max(0, input_total - cached - cache_write)
    cache_read_rate = (
        pricing.cache_read if pricing.cache_read is not None else pricing.input
    )
    cache_write_rate = (
        pricing.cache_write if pricing.cache_write is not None else pricing.input * 1.25
    )

    return (
        uncached_input * pricing.input
        + cached * cache_read_rate
        + cache_write * cache_write_rate
        + output_total * pricing.output
    )


def settle_cost_usd(
    native_cost_usd: float | None,
    *,
    native_cost_trusted: bool,
    model: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_tokens: int | None = None,
    cache_write_tokens: int | None = None,
) -> float | None:
    usable = (
        native_cost_trusted
        and native_cost_usd is not None
        and math.isfinite(native_cost_usd)
        and native_cost_usd >= 0
    )
    if usable and native_cost_usd:
        return native_cost_usd
    if not (input_tokens or output_tokens or cache_write_tokens):
        return native_cost_usd if usable else None
    estimated = estimate_cost_usd(
        model, input_tokens, output_tokens, cache_tokens, cache_write_tokens
    )
    if estimated is None or not math.isfinite(estimated):
        return None
    return estimated

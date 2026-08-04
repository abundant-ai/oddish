"""Fingerprinting for LLM-provider-key cost exclusion.

The exclusion feature never uses an LLM key, it only recognizes one, so a
one-way SHA-256 hash is the whole identity: the admin list stores
``hash_llm_key(pasted_key)`` and every finished trial is stamped with the hash
of the platform key it ran on (:func:`platform_key_hash_for_provider`). Equal
hashes mean the same key. The plaintext is never stored.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping


def hash_llm_key(raw_key: str) -> str:
    """SHA-256 hex of an LLM API key."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


def key_hint(raw_key: str) -> str:
    """Masked last-4 tail for display, e.g. ``xai-…9f2c`` renders from ``9f2c``."""
    return raw_key[-4:]


# Providers Oddish routes with its own platform credentials, which Harbor's
# ``PROVIDER_KEYS`` either doesn't know at all (zai, minimax, anthropic-hdo)
# or maps to a variable the worker doesn't export (azure -> AZURE_API_KEY is
# built for the agent env only; the worker process holds
# AZURE_OPENAI_API_KEY). Mirrors the worker's wiring in
# ``workers/harbor/agent_config.py`` / ``config.Settings``.
_ODDISH_PROVIDER_ENV_KEYS: dict[str, str] = {
    "anthropic-hdo": "ANTHROPIC_HDO_API_KEY",
    "azure": "AZURE_OPENAI_API_KEY",
    # Both spellings of the Azure transport, for the same reason the runner's
    # redaction map lists both: ``azure_openai`` is not in the normalizer's
    # provider set, so it can pass through verbatim, and a map keyed on only
    # ``azure`` would leave those trials unstamped.
    "azure_openai": "AZURE_OPENAI_API_KEY",
    # Harbor's AWS_ACCESS_KEY_ID entry is Oddish's storage credential, not the
    # bearer token that funds Bedrock model calls.
    "bedrock": "AWS_BEARER_TOKEN_BEDROCK",
    "meta": "META_API_KEY",
    "zai": "ZAI_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
}


def _oddish_platform_key(provider: str) -> str | None:
    """Key for a provider Oddish wires itself (``_ODDISH_PROVIDER_ENV_KEYS``).

    Settings first, then the environment, matching how the worker resolves
    these keys when it launches the trial (``_resolve_anthropic_hdo_api_key``,
    ``require_azure_openai_config``); the remaining providers are exported to
    the agent purely from the worker's environment.
    """
    from oddish.config import settings

    configured = {
        "anthropic-hdo": settings.anthropic_hdo_api_key,
        "azure": settings.azure_openai_api_key,
        "azure_openai": settings.azure_openai_api_key,
        "meta": settings.meta_api_key,
    }.get(provider)
    return configured or os.environ.get(_ODDISH_PROVIDER_ENV_KEYS[provider])


def _openai_platform_key(model: str | None = None) -> str | None:
    """Key for the OpenAI-family route this *model* resolves to, or None.

    Per-model: an explicit ``openai/`` id is funded by the public platform
    key and ``azure/`` by the Azure key; a bare id follows the
    ODDISH_OPENAI_PROVIDER default (``get_openai_route_for_model``).
    """
    from oddish.config import settings

    try:
        route = settings.get_openai_route_for_model(model)
    except ValueError:
        return os.environ.get("OPENAI_API_KEY")

    if route == "azure":
        return settings.azure_openai_api_key or os.environ.get("AZURE_OPENAI_API_KEY")
    return settings.openai_api_key or os.environ.get("OPENAI_API_KEY")


def _provider_key_var(provider: str) -> str | None:
    """Canonical env-var name holding ``provider``'s API key, or None."""
    var = _ODDISH_PROVIDER_ENV_KEYS.get(provider)
    if var:
        return var
    from harbor.agents.utils import PROVIDER_KEYS

    mapped = PROVIDER_KEYS.get(provider)
    if isinstance(mapped, list):
        mapped = mapped[0] if mapped else None
    return mapped or None


def trial_llm_key_hash(
    provider: str | None,
    byok_env: Mapping[str, str] | None = None,
    model: str | None = None,
) -> str | None:
    """Hash of the key that funded one trial.

    A BYOK overlay replaces the platform credential inside the agent env, so
    when it carries the provider's key variable -- or ``ANTHROPIC_API_KEY``,
    the variable every direct-Anthropic reroute (including claude-code on a
    Bedrock-canonicalized model) injects -- the trial ran, and was paid for, on
    that user key: hash it, so listing the platform key can never swallow BYOK
    spend and a pasted BYOK key matches. With no overlay the platform key
    funded the run (:func:`platform_key_hash_for_provider`).
    """
    from oddish.config import is_anthropic_platform_model

    # An explicit ``anthropic/`` id pins the platform ANTHROPIC_API_KEY in the
    # runner (the prefix wins over BYOK, exactly like anthropic-hdo/), so a
    # resolved BYOK overlay never funds the run — stamp the platform key.
    # anthropic-hdo/ needs no guard: its var is ANTHROPIC_HDO_API_KEY, which a
    # BYOK overlay never carries.
    if byok_env and provider and not is_anthropic_platform_model(model):
        var = _provider_key_var(provider)
        raw = byok_env.get(var) if var else None
        # HDO wins over BYOK in the runner when its model prefix opts in.
        if not raw and provider in ("anthropic", "bedrock"):
            raw = byok_env.get("ANTHROPIC_API_KEY")
        raw = (raw or "").strip()
        if raw:
            return hash_llm_key(raw)
    return platform_key_hash_for_provider(provider, model=model)


def platform_key_hash_for_provider(
    provider: str | None, model: str | None = None
) -> str | None:
    """Hash of the platform API key configured for ``provider``, or None.

    An Oddish-wired provider resolves only through :func:`_oddish_platform_key`
    -- its map entry is how the worker actually funds the trial, so falling
    back to Harbor's variable would hash a key the trial never used (azure).
    Everything else reads the worker's environment via Harbor's
    ``PROVIDER_KEYS`` map (the same env var the runtime authenticates with).
    Fail-open: an unknown provider or an unset key yields None so the trial is
    simply left unstamped -- and therefore never excluded -- rather than failing.
    """
    if not provider:
        return None

    try:
        if provider == "openai":
            raw = _openai_platform_key(model)
        elif provider in _ODDISH_PROVIDER_ENV_KEYS:
            raw = _oddish_platform_key(provider)
        else:
            var = _provider_key_var(provider)
            if not var:
                return None
            raw = os.environ.get(var)
    except Exception:
        return None

    raw = (raw or "").strip()
    return hash_llm_key(raw) if raw else None

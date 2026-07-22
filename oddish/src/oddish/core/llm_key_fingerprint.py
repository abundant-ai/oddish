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
    "meta": "META_API_KEY",
    "zai": "ZAI_API_KEY",
    "minimax": "MINIMAX_API_KEY",
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
        "meta": settings.meta_api_key,
    }.get(provider)
    return configured or os.environ.get(_ODDISH_PROVIDER_ENV_KEYS[provider])


def platform_key_hash_for_provider(provider: str | None) -> str | None:
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

    if provider in _ODDISH_PROVIDER_ENV_KEYS:
        raw = _oddish_platform_key(provider)
    else:
        from harbor.agents.utils import PROVIDER_KEYS

        var = PROVIDER_KEYS.get(provider)
        if isinstance(var, list):
            var = var[0] if var else None
        if not var:
            return None
        raw = os.environ.get(var)

    raw = (raw or "").strip()
    return hash_llm_key(raw) if raw else None

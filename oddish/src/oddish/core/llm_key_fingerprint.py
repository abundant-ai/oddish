"""One-way identity for the provider key that funded a trial."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping

_ODDISH_PROVIDER_ENV_KEYS = {
    "anthropic-hdo": "ANTHROPIC_HDO_API_KEY",
    "azure": "AZURE_OPENAI_API_KEY",
    "bedrock": "AWS_BEARER_TOKEN_BEDROCK",
    "meta": "META_API_KEY",
    "geometric": "GEOMETRIC_API_KEY",
    "zai": "ZAI_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
}


def hash_llm_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def key_hint(raw_key: str) -> str:
    return raw_key[-4:]


def _provider_key_var(provider: str) -> str | None:
    if provider in _ODDISH_PROVIDER_ENV_KEYS:
        return _ODDISH_PROVIDER_ENV_KEYS[provider]
    from harbor.agents.utils import PROVIDER_KEYS

    mapped = PROVIDER_KEYS.get(provider)
    if isinstance(mapped, list):
        return mapped[0] if mapped else None
    return mapped or None


def platform_key_hash_for_provider(provider: str | None) -> str | None:
    """Hash the platform credential Oddish actually wires for ``provider``."""
    if not provider:
        return None
    try:
        from oddish.config import settings

        if provider == "openai":
            try:
                route = settings.get_openai_provider()
            except ValueError:
                route = "openai"
            raw = (
                settings.azure_openai_api_key or os.environ.get("AZURE_OPENAI_API_KEY")
                if route == "azure"
                else settings.openai_api_key or os.environ.get("OPENAI_API_KEY")
            )
        elif provider == "anthropic-hdo":
            raw = settings.anthropic_hdo_api_key or os.environ.get(
                "ANTHROPIC_HDO_API_KEY"
            )
        elif provider == "azure":
            raw = settings.azure_openai_api_key or os.environ.get(
                "AZURE_OPENAI_API_KEY"
            )
        elif provider == "meta":
            raw = settings.meta_api_key or os.environ.get("META_API_KEY")
        elif provider == "geometric":
            raw = settings.geometric_api_key or os.environ.get("GEOMETRIC_API_KEY")
        else:
            variable = _provider_key_var(provider)
            raw = os.environ.get(variable) if variable else None
    except Exception:
        # Fingerprinting is accounting metadata; an unknown provider must not
        # prevent the trial itself from running.
        return None
    raw = (raw or "").strip()
    return hash_llm_key(raw) if raw else None


def trial_llm_key_hash(
    provider: str | None, byok_env: Mapping[str, str] | None = None
) -> str | None:
    """Hash the BYOK credential when present, otherwise the platform key."""
    if byok_env and provider:
        variable = _provider_key_var(provider)
        raw = byok_env.get(variable) if variable else None
        if not raw and provider in {"anthropic", "bedrock"}:
            raw = byok_env.get("ANTHROPIC_API_KEY")
        raw = (raw or "").strip()
        if raw:
            return hash_llm_key(raw)
    return platform_key_hash_for_provider(provider)

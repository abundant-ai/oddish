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


def platform_key_hash_for_provider(provider: str | None) -> str | None:
    """Hash of the platform API key configured for ``provider``, or None.

    Reads the provider's key from the worker's environment via Harbor's
    ``PROVIDER_KEYS`` map (the same env var the runtime authenticates with).
    Fail-open: an unknown provider or an unset key yields None so the trial is
    simply left unstamped -- and therefore never excluded -- rather than failing.
    """
    if not provider:
        return None
    from harbor.agents.utils import PROVIDER_KEYS

    var = PROVIDER_KEYS.get(provider)
    if isinstance(var, list):
        var = var[0] if var else None
    if not var:
        return None
    raw = os.environ.get(var)
    return hash_llm_key(raw) if raw else None

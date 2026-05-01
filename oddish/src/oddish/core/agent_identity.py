from __future__ import annotations

import hashlib


def compute_agent_equivalence_key(
    harness: str,
    model: str,
    provider: str,
) -> str:
    """Return the stable evidence-pooling key for an agent configuration."""
    payload = f"{harness}|{model}|{provider}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

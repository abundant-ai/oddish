"""Agent identity and equivalence keying.

Two trials are fungible evidence of the same thing iff their
``(task_version_id, agent_equivalence_key)`` matches. The equivalence
key is the canonical hash of an agent's identity tuple.

Bundle identity is a separate axis, carried by ``TaskVersion.content_hash``
(the hash of the task bundle zip). Harbor passthrough overrides at
submission time are deliberately *not* part of the equivalence key --
the bundle is the source of truth for what the agent saw.
"""

from __future__ import annotations

import hashlib


def compute_agent_equivalence_key(
    harness: str,
    model: str | None,
    provider: str,
) -> str:
    """Return the canonical equivalence key for an agent identity.

    Inputs are normalized (stripped, lowercased) before hashing so
    cosmetic casing differences don't fragment evidence pools.
    """

    parts = [
        (harness or "").strip().lower(),
        (model or "").strip().lower(),
        (provider or "").strip().lower(),
    ]
    payload = "|".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()

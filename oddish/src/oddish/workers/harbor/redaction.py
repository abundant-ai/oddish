"""Exact-value redaction shared by the runner and live-tail secret handling.

Restricted-Compose trials stage a map of worker-private values -> placeholders
(built in ``runner._runtime_transport_redactions``). Two call paths must apply
that map identically: the lifecycle-hook redaction in ``runner`` and the
persisted live-tail row redaction in ``live_tail``. Keeping ONE implementation
here means a secret-handling fix cannot diverge between them -- parallel copies
previously produced a short-circuit bug in live-tail's ``_sanitize_event``.

``redact_exact_value`` handles both the plain JSON payloads live-tail persists
(str / mapping / list / tuple) and the richer lifecycle shapes the runner scrubs
(pydantic ``BaseModel`` fields, ``SecretStr`` / ``SecretBytes``, sets), bounded
by a recursion-depth guard. Non-JSON branches are simply never exercised by the
live-tail payloads, so unifying is behaviour-preserving for both callers.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, SecretBytes, SecretStr

from .restricted_network import RUNTIME_ALLOWED_HOSTS_ATTR, RUNTIME_MODEL_NAME_ATTR

_MAX_REDACTION_DEPTH = 32


def redact_exact_text(text: str, replacements: dict[str, str]) -> str:
    """Replace each staged exact value in *text*, longest match first."""
    for value in sorted(replacements, key=len, reverse=True):
        text = text.replace(value, replacements[value])
    return text


def redact_exact_bytes(data: bytes, replacements: dict[str, str]) -> bytes:
    """Replace each staged exact value in raw *data* (utf-8), longest first."""
    for value in sorted(replacements, key=len, reverse=True):
        data = data.replace(value.encode("utf-8"), replacements[value].encode("utf-8"))
    return data


def redact_exact_value(
    value: Any,
    replacements: dict[str, str],
    *,
    _depth: int = 0,
) -> Any:
    """Copy a value tree while replacing trial-private exact values."""
    if isinstance(value, str):
        return redact_exact_text(value, replacements)
    if isinstance(value, SecretStr):
        return SecretStr(redact_exact_text(value.get_secret_value(), replacements))
    if isinstance(value, SecretBytes):
        raw = value.get_secret_value()
        for exact, replacement in sorted(
            replacements.items(), key=lambda item: len(item[0]), reverse=True
        ):
            raw = raw.replace(exact.encode(), replacement.encode())
        return SecretBytes(raw)
    if _depth > _MAX_REDACTION_DEPTH:
        return (
            "[REDACTION_DEPTH_LIMIT]"
            if isinstance(value, (BaseModel, Mapping, list, tuple, set, frozenset))
            else value
        )
    if isinstance(value, BaseModel):
        updates = {
            name: redact_exact_value(
                getattr(value, name), replacements, _depth=_depth + 1
            )
            for name in type(value).model_fields
            if name != "environment"
        }
        redacted_model = value.model_copy(update=updates)
        for attribute in (RUNTIME_ALLOWED_HOSTS_ATTR, RUNTIME_MODEL_NAME_ATTR):
            if hasattr(redacted_model, attribute):
                object.__delattr__(redacted_model, attribute)
        return redacted_model
    if isinstance(value, Mapping):
        return {
            redact_exact_value(key, replacements, _depth=_depth + 1): (
                redact_exact_value(item, replacements, _depth=_depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            redact_exact_value(item, replacements, _depth=_depth + 1) for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            redact_exact_value(item, replacements, _depth=_depth + 1) for item in value
        )
    if isinstance(value, (set, frozenset)):
        redacted = {
            redact_exact_value(item, replacements, _depth=_depth + 1) for item in value
        }
        return frozenset(redacted) if isinstance(value, frozenset) else redacted
    return value

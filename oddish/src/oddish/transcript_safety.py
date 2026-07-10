from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

_PRESERVED_CONTROLS = {"\n", "\r", "\t"}


@dataclass(frozen=True)
class SanitizedText:
    text: str
    changed: bool
    replacements: int = 0


@dataclass(frozen=True)
class SanitizedValue:
    value: Any
    changed: bool
    replacements: int = 0


def sanitize_transcript_text(value: str) -> SanitizedText:
    """Render DB/terminal-dangerous characters as visible, inert text."""
    out: list[str] = []
    changed = False
    replacements = 0
    for ch in value:
        code = ord(ch)
        replacement: str | None = None
        if ch in _PRESERVED_CONTROLS:
            out.append(ch)
            continue
        if 0xD800 <= code <= 0xDFFF:
            replacement = "\ufffd"
        elif code == 0x7F:
            replacement = "\u2421"
        elif code < 0x20:
            replacement = chr(0x2400 + code)
        elif 0x80 <= code <= 0x9F:
            replacement = f"\\u{code:04X}"

        if replacement is None:
            out.append(ch)
        else:
            out.append(replacement)
            changed = True
            replacements += 1
    if not changed:
        return SanitizedText(value, changed=False)
    return SanitizedText("".join(out), changed=True, replacements=replacements)


def sanitize_transcript_value(value: Any) -> SanitizedValue:
    """Return a JSON-compatible value with unsafe transcript strings normalized."""
    if isinstance(value, str):
        text = sanitize_transcript_text(value)
        return SanitizedValue(text.text, text.changed, text.replacements)
    if isinstance(value, dict):
        changed = False
        replacements = 0
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            safe_key = sanitize_transcript_text(str(key))
            safe_item = sanitize_transcript_value(item)
            normalized[safe_key.text] = safe_item.value
            changed = changed or safe_key.changed or safe_item.changed
            replacements += safe_key.replacements + safe_item.replacements
        return SanitizedValue(normalized, changed, replacements)
    if isinstance(value, (list, tuple, set)):
        changed = not isinstance(value, list)
        replacements = 0
        normalized_list: list[Any] = []
        for item in value:
            safe_item = sanitize_transcript_value(item)
            normalized_list.append(safe_item.value)
            changed = changed or safe_item.changed
            replacements += safe_item.replacements
        return SanitizedValue(normalized_list, changed, replacements)
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return SanitizedValue(value, changed=False)
    if isinstance(value, float):
        if math.isfinite(value):
            return SanitizedValue(value, changed=False)
        return SanitizedValue(str(value), changed=True)

    safe = sanitize_transcript_text(str(value))
    return SanitizedValue(safe.text, changed=True, replacements=safe.replacements)

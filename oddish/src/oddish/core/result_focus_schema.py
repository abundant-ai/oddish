"""Detect + normalize the operator's ``result_focus`` JSON Schema.

``result_focus`` is reused as either a prose question (plain text) or a JSON
Schema (a top-level JSON object). When it's a schema we enforce it at analysis
time via the Anthropic API's structured outputs, which only accept a subset of
JSON Schema — so we reject the unsupported constructs here, at save time, with a
clear message rather than failing mid-analysis.
"""

from __future__ import annotations

import copy
import json
import logging

import jsonschema

logger = logging.getLogger(__name__)

# Keys the structured-outputs subset does not support (see plan Global Constraints).
_UNSUPPORTED_KEYS = frozenset(
    {"minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum",
     "exclusiveMaximum", "multipleOf", "minItems", "maxItems", "pattern", "$ref"}
)


class UnsupportedSchemaError(ValueError):
    """Raised when a result_focus schema uses a construct the API can't enforce."""


def parse_result_focus(result_focus: str | None) -> dict | None:
    """Return the parsed schema dict if ``result_focus`` is a JSON object, else None.

    Deterministic and synchronous — no LLM. The best-effort LLM repair of a
    malformed-but-JSON-ish value lives in the async wrapper layer
    (``result_focus_repair.repair_result_focus_if_needed``); callers run that
    first, then parse the repaired string here."""
    if not result_focus or not result_focus.strip():
        return None
    try:
        parsed = json.loads(result_focus)
    except (ValueError, TypeError) as exc:
        # A prose focus question legitimately isn't JSON (callers handle None), so
        # only flag an error when the input clearly meant to be JSON; otherwise
        # debug to avoid burying real failures under every prose probe. Either way
        # capture the exception and the value we tried to parse.
        if result_focus.lstrip().startswith(("{", "[")):
            logger.error(
                "Could not parse result_focus as JSON (%s: %s): %r",
                type(exc).__name__,
                exc,
                result_focus,
            )
        else:
            logger.debug(
                "result_focus is prose, not JSON (%s: %s): %r",
                type(exc).__name__,
                exc,
                result_focus,
            )
        return None
    return parsed if isinstance(parsed, dict) else None


def normalize_findings_schema(spec: dict) -> dict:
    """Deep-copy ``spec``, verify it's a well-formed JSON Schema, insert
    ``additionalProperties: false`` on every object, and raise
    ``UnsupportedSchemaError`` on a malformed or unsupported construct."""
    out = copy.deepcopy(spec)
    try:
        jsonschema.Draft7Validator.check_schema(out)
    except jsonschema.exceptions.SchemaError as exc:
        raise UnsupportedSchemaError(f"invalid JSON Schema: {exc.message}") from exc
    _walk(out)
    return out


def _walk(node: object) -> None:
    if isinstance(node, dict):
        bad = _UNSUPPORTED_KEYS.intersection(node)
        if bad:
            raise UnsupportedSchemaError(
                f"unsupported JSON Schema key(s): {', '.join(sorted(bad))}"
            )
        if node.get("type") == "object":
            node.setdefault("additionalProperties", False)
        for value in node.values():
            _walk(value)
    elif isinstance(node, list):
        for item in node:
            _walk(item)

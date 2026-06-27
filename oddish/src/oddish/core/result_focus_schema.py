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
    {
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
        "pattern",
        "$ref",
    }
)


class UnsupportedSchemaError(ValueError):
    """Raised when a result_focus schema uses a construct the API can't enforce."""


def _parse_json_object(result_focus: str | None) -> dict | None:
    """Deterministic sync parse: the parsed dict if ``result_focus`` is a JSON
    object, else None. Logs every failure (this is where "what failed to parse"
    is recorded). Pure — no LLM, so it's safe to call from sync code and from the
    repair machinery's own predicates without recursion."""
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


async def parse_result_focus(
    result_focus: str | None,
    *,
    client=None,
    kind: str = "output_spec",
    repair: bool = True,
) -> dict | None:
    """Parse ``result_focus`` as a JSON object, repairing malformed input via a
    cheap LLM pass when ``repair`` is set (the default).

    The deterministic parse (:func:`_parse_json_object`) is tried first. Only
    JSON-*intended-but-malformed* input — it starts with ``{``/``[`` yet
    ``json.loads`` raises — is worth an LLM repair; valid JSON that simply isn't
    an object (``"[1,2]"``, ``"42"``) and prose questions return None untouched.
    ``kind`` selects the repair target (see ``RepairKind`` in
    ``result_focus_repair``). Pass ``repair=False`` for a deterministic
    sync-equivalent that never calls the LLM.
    """
    parsed = _parse_json_object(result_focus)
    if parsed is not None or not repair or not result_focus:
        return parsed
    body = result_focus.strip()
    if not body.startswith(("{", "[")):
        return None  # prose focus question, not a JSON spec
    try:
        json.loads(body)
        return None  # valid JSON but not an object -> nothing to repair
    except (ValueError, TypeError):
        pass  # genuinely malformed -> attempt an LLM repair
    from oddish.core.result_focus_repair import repair_result_focus_json

    repaired, _ = await repair_result_focus_json(body, client=client, kind=kind)
    return repaired


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

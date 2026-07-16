"""Best-effort LLM repair of a malformed ``result_focus`` JSON output spec.

When an operator's ``result_focus`` is meant to be a JSON object but doesn't
parse, a small, cheap model (Haiku) is asked to coerce it into the intended JSON
before we fall back to rendering it verbatim. This is a rare error path (it only
fires when the input is JSON-ish but unparseable), so the extra latency/cost is
negligible in the normal case. Every failure mode is swallowed and logged so the
probe overlay always still renders.
"""

from __future__ import annotations

import json
import logging
from typing import Literal

from oddish.core.result_focus_schema import parse_result_focus

logger = logging.getLogger(__name__)

# ``result_focus`` is reused as JSON in two distinct ways and a malformed value
# needs a different repair target for each:
#   - "output_spec": the probe overlay renders it as the exact JSON the probe must
#     emit, so we repair toward a single valid JSON *object/example*.
#   - "schema": the analyzer feeds it to the Anthropic structured-outputs envelope,
#     so we repair toward a single valid JSON *Schema* (type/properties/required).
RepairKind = Literal["output_spec", "schema"]

_REPAIR_PROMPT_OUTPUT_SPEC = (
    "An operator supplied the text below as a JSON output specification — an "
    "example of the exact JSON object they want produced — but it does not parse "
    "as valid JSON. Repair it into a single valid JSON object that preserves their "
    "intended fields and structure as closely as possible. Output ONLY the JSON "
    "object — no prose, no markdown, no code fences.\n\n"
    "--- BEGIN result_focus ---\n{raw}\n--- END result_focus ---"
)

_REPAIR_PROMPT_SCHEMA = (
    "An operator supplied the text below as a JSON Schema describing the required "
    "shape of an analysis result, but it does not parse as valid JSON. Repair it "
    'into a single valid JSON Schema object (Draft-7 style: a top-level "type", '
    '"properties", and optionally "required"). Preserve their intended property '
    "names, types, and nesting as closely as possible; do not invent constraints "
    "or example values. Output ONLY the JSON object — no prose, no markdown, no "
    "code fences.\n\n"
    "--- BEGIN result_focus ---\n{raw}\n--- END result_focus ---"
)

_REPAIR_PROMPTS: dict[str, str] = {
    "output_spec": _REPAIR_PROMPT_OUTPUT_SPEC,
    "schema": _REPAIR_PROMPT_SCHEMA,
}

# Last-resort extraction: when even the repaired output won't parse deterministically,
# ask the model to pull just the JSON object back out of its own messy text.
_EXTRACT_PROMPT = (
    "The text below is supposed to contain a single JSON object, but it may be "
    "wrapped in prose, markdown, or code fences, or be slightly malformed. Extract "
    "the intended JSON object and return it as a single valid JSON value, preserving "
    "its fields and structure. Output ONLY the JSON object — no prose, no markdown, "
    "no code fences.\n\n"
    "--- BEGIN text ---\n{raw}\n--- END text ---"
)


def _extract_object(text: str) -> dict | None:
    """Parse ``text`` as a JSON object, tolerating surrounding prose/fences."""
    obj = parse_result_focus(text)
    if obj is not None:
        return obj
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return parse_result_focus(text[start : end + 1])
    return None


async def _extract_object_llm(text: str) -> dict | None:
    """LLM fallback for :func:`_extract_object`: ask the model to extract the JSON.

    Used only when deterministic extraction fails on the repaired output. Never
    raises — any error (network, still-unparseable) yields ``None`` so the caller
    falls back to its existing best-effort path.
    """
    from oddish.config import settings
    from oddish.core.llm import complete

    try:
        result = await complete(
            handler="result_focus_repair",
            prompt=_EXTRACT_PROMPT.format(raw=text),
            model=settings.repair_model,
            max_tokens=2048,
        )
    except Exception:
        logger.exception("result_focus LLM extraction fallback failed for %r", text)
        return None
    return _extract_object(result.text.strip())


async def repair_result_focus_json(
    raw: str, *, kind: RepairKind = "output_spec"
) -> tuple[dict | None, str]:
    """Ask a cheap model to coerce malformed ``raw`` into a JSON object.

    ``kind`` selects the repair target (see ``RepairKind``): ``"output_spec"``
    repairs toward a literal JSON object/example, ``"schema"`` toward a valid JSON
    Schema.

    Returns ``(parsed_object_or_None, raw_llm_text)``. Never raises: on any error
    (missing API key, network failure, still-unparseable output) it returns
    ``(None, llm_text_or_empty)`` so callers fall back to their existing
    best-effort path.
    """
    from oddish.config import settings
    from oddish.core.llm import complete

    llm_output = ""
    try:
        result = await complete(
            handler="result_focus_repair",
            prompt=_REPAIR_PROMPTS[kind].format(raw=raw),
            model=settings.repair_model,
            max_tokens=2048,
        )
        llm_output = result.text.strip()
    except Exception:
        logger.exception("result_focus LLM repair call failed for %r", raw)
        return None, llm_output

    logger.warning("we failed to parse %s and the llm outputted: %s", raw, llm_output)
    obj = _extract_object(llm_output)
    if obj is None and llm_output:
        # Deterministic extraction failed too -> one more LLM pass to salvage it.
        obj = await _extract_object_llm(llm_output)
    return obj, llm_output


async def repair_result_focus_if_needed(
    result_focus: str | None, *, kind: RepairKind = "output_spec"
) -> str | None:
    """Return an effective ``result_focus``, repairing if needed.

    ``kind`` selects the repair target for the JSON-ish-but-unparseable case (see
    ``RepairKind``): the probe overlay passes ``"output_spec"``, the analyzer
    passes ``"schema"``.

    - Already-valid JSON or a prose focus question: returned unchanged.
    - JSON-ish but unparseable: attempt an LLM repair; on success return the
      repaired JSON string, otherwise return the original (callers still do a
      best-effort render / fall back to prose mode).
    """
    if not result_focus or not result_focus.strip():
        return result_focus
    body = result_focus.strip()
    if parse_result_focus(body) is not None:
        return result_focus
    if not body.startswith(("{", "[")):
        return result_focus  # prose focus question, not a JSON spec
    # JSON-intended but unparseable. This is the single funnel every probe consumer
    # passes through before the deterministic leaf parser, so repairing once here
    # covers the whole downstream chain (overlay render, schema analysis).
    logger.warning("result_focus did not parse as JSON; queuing LLM repair: %r", body)
    repaired, _ = await repair_result_focus_json(body, kind=kind)
    return json.dumps(repaired) if repaired is not None else result_focus

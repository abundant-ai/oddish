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

from oddish.core.result_focus_schema import parse_result_focus

logger = logging.getLogger(__name__)

# A tiny JSON-repair task -> cheapest/fastest tier. Plain API id: repair always
# runs on the direct Anthropic API (ANTHROPIC_API_KEY), like the probe summary.
REPAIR_MODEL = "claude-haiku-4-5"

_REPAIR_PROMPT = (
    "An operator supplied the text below as a JSON output specification, but it "
    "does not parse as valid JSON. Repair it into a single valid JSON object that "
    "preserves their intent as closely as possible. Output ONLY the JSON object — "
    "no prose, no markdown, no code fences.\n\n"
    "--- BEGIN result_focus ---\n{raw}\n--- END result_focus ---"
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


async def repair_result_focus_json(raw: str, *, client=None) -> tuple[dict | None, str]:
    """Ask a cheap model to coerce malformed ``raw`` into a JSON object.

    Returns ``(parsed_object_or_None, raw_llm_text)``. Never raises: on any error
    (missing API key, network failure, still-unparseable output) it returns
    ``(None, llm_text_or_empty)`` so callers fall back to their existing
    best-effort path.
    """
    llm_output = ""
    try:
        if client is None:
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic()
        msg = await client.messages.create(
            model=REPAIR_MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": _REPAIR_PROMPT.format(raw=raw)}],
        )
        for block in msg.content:
            if hasattr(block, "text"):
                llm_output += block.text
        llm_output = llm_output.strip()
    except Exception:
        logger.exception("result_focus LLM repair call failed for %r", raw)
        return None, llm_output

    logger.warning("we failed to parse %s and the llm outputted: %s", raw, llm_output)
    return _extract_object(llm_output), llm_output


async def repair_result_focus_if_needed(
    result_focus: str | None, *, client=None
) -> str | None:
    """Return an effective ``result_focus`` for rendering, repairing if needed.

    - Already-valid JSON or a prose focus question: returned unchanged.
    - JSON-ish but unparseable: attempt an LLM repair; on success return the
      repaired JSON string, otherwise return the original (the renderer still
      does a verbatim best-effort render).
    """
    if not result_focus or not result_focus.strip():
        return result_focus
    body = result_focus.strip()
    if parse_result_focus(body) is not None:
        return result_focus
    if not body.startswith("{"):
        return result_focus  # prose focus question, not a JSON spec
    repaired, _ = await repair_result_focus_json(body, client=client)
    return json.dumps(repaired) if repaired is not None else result_focus

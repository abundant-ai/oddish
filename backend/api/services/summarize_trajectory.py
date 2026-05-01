"""LLM-backed trajectory summarization.

Two pure-ish responsibilities:
  - ``preprocess`` strips image content parts and truncates large text fields
    so the token cost of the summary call is bounded.
  - ``generate`` calls the Anthropic API with a preprocessed trajectory and
    returns a persistable summary dict.

This module deliberately mirrors the JSON-parsing style of
``oddish.worker.local_runner._run_probe_analyzer`` rather than using tool-use
so the test patterns and prompt-shape conventions match the rest of the repo.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

MAX_TEXT_CHARS = 2000
TRUNCATE_HEAD = 800
TRUNCATE_TAIL = 400
TRUNCATION_MARKER = "\n[...truncated {n} chars...]\n"
SCHEMA_VERSION = "1"
MODEL = "claude-sonnet-4-6"


def _truncate(text: str) -> str:
    if len(text) <= MAX_TEXT_CHARS:
        return text
    head = text[:TRUNCATE_HEAD]
    tail = text[-TRUNCATE_TAIL:]
    omitted = len(text) - TRUNCATE_HEAD - TRUNCATE_TAIL
    return head + TRUNCATION_MARKER.format(n=omitted) + tail


def _strip_images(parts: list[dict]) -> list[dict]:
    """Replace image parts with a single placeholder text part."""
    out: list[dict] = []
    skipped = 0
    for part in parts:
        if isinstance(part, dict) and part.get("type") == "image":
            skipped += 1
            continue
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text") or ""
            out.append({"type": "text", "text": _truncate(text)})
        else:
            out.append(part)
    if skipped:
        out.append({"type": "text", "text": f"[image omitted] (x{skipped})"})
    return out


def _process_content(value: Any) -> Any:
    """Process MessageContent / ObservationContent (string | list[ContentPart] | None)."""
    if value is None:
        return None
    if isinstance(value, str):
        return _truncate(value)
    if isinstance(value, list):
        return _strip_images(value)
    return value


def _process_tool_calls(tool_calls: list[dict] | None) -> list[dict] | None:
    if not tool_calls:
        return tool_calls
    out = []
    for call in tool_calls:
        new_call = dict(call)
        args = new_call.get("arguments")
        if isinstance(args, dict):
            new_call["arguments"] = {
                k: _truncate(v) if isinstance(v, str) else v
                for k, v in args.items()
            }
        out.append(new_call)
    return out


def _process_observation(obs: dict | None) -> dict | None:
    if obs is None:
        return None
    new_obs = dict(obs)
    new_results = []
    for result in obs.get("results") or []:
        new_result = dict(result)
        new_result["content"] = _process_content(result.get("content"))
        new_results.append(new_result)
    new_obs["results"] = new_results
    return new_obs


def preprocess(trajectory: dict) -> dict:
    """Return a copy of ``trajectory`` with images stripped and long text truncated."""
    out = deepcopy(trajectory)
    new_steps = []
    for step in out.get("steps") or []:
        new_step = dict(step)
        new_step["message"] = _process_content(step.get("message"))
        rc = step.get("reasoning_content")
        if isinstance(rc, str):
            new_step["reasoning_content"] = _truncate(rc)
        new_step["tool_calls"] = _process_tool_calls(step.get("tool_calls"))
        new_step["observation"] = _process_observation(step.get("observation"))
        new_steps.append(new_step)
    out["steps"] = new_steps
    return out


class SummaryGenerationError(RuntimeError):
    """Raised when the LLM returned content we could not turn into a summary."""


_PROMPT_HEADER = (
    "You are summarizing a recorded agent trajectory for a developer who "
    "wants a quick scan before diving into the per-step view. Produce a "
    "2-3 sentence summary covering what the agent set out to do and how "
    "it ended, then 3-6 pivotal 'key moments' with their step ids.\n\n"
    "Each highlight must reference a real `step_id` from the trajectory below. "
    "Pick steps where something genuinely shifted: a strategy was committed, "
    "a key tool call landed, an error redirected the work, or the final "
    "verdict was reached. Skip filler.\n\n"
    "Respond with ONLY a JSON object (no preamble, no code fences) matching "
    "this exact shape:\n"
    "{\n"
    '  "summary": "2-3 sentences",\n'
    '  "highlights": [\n'
    '    {"step_id": <int>, "title": "<short label>", "why": "<one sentence>"}\n'
    "  ]\n"
    "}\n"
    "Highlights must be ordered by step_id ascending.\n\n"
)


def _strip_code_fences(text: str) -> str:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.lstrip().startswith("json"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        raw = raw.rsplit("```", 1)[0]
    return raw.strip()


async def generate(trajectory: dict) -> dict:
    """Call Claude to produce a persistable summary dict for ``trajectory``.

    Raises ``SummaryGenerationError`` if the model returns malformed JSON or
    cannot be parsed. Highlights referencing step_ids that are not in the
    source trajectory are dropped silently.
    """
    from anthropic import AsyncAnthropic

    valid_step_ids = {
        step.get("step_id")
        for step in (trajectory.get("steps") or [])
        if isinstance(step.get("step_id"), int)
    }

    compact = preprocess(trajectory)
    prompt = _PROMPT_HEADER + "<trajectory>\n" + json.dumps(compact) + "\n</trajectory>"

    try:
        client = AsyncAnthropic()
        msg = await client.messages.create(
            model=MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        raise SummaryGenerationError(f"Anthropic API call failed: {e}") from e

    raw_text = ""
    for block in msg.content:
        if hasattr(block, "text"):
            raw_text += block.text
    raw_text = _strip_code_fences(raw_text)
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise SummaryGenerationError(f"Model returned non-JSON: {e}") from e

    if not isinstance(parsed, dict):
        raise SummaryGenerationError(
            f"Model returned {type(parsed).__name__}, expected object"
        )

    summary = str(parsed.get("summary") or "").strip()
    raw_highlights = parsed.get("highlights") or []
    highlights: list[dict] = []
    if isinstance(raw_highlights, list):
        for entry in raw_highlights:
            if not isinstance(entry, dict):
                continue
            step_id = entry.get("step_id")
            if not isinstance(step_id, int) or step_id not in valid_step_ids:
                continue
            highlights.append(
                {
                    "step_id": step_id,
                    "title": str(entry.get("title") or "").strip(),
                    "why": str(entry.get("why") or "").strip(),
                }
            )

    return {
        "schema_version": SCHEMA_VERSION,
        "model": MODEL,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "highlights": highlights,
    }

"""Render Claude Code stream-json events as human-readable log lines.

Shared by the prototype harness (backend/scripts/haiku_sandbox_bad_failures.py)
and the sandboxed analyzer, so the two can't drift.
"""

import json


def tool_result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                out.append(b.get("text", ""))
            else:
                out.append(json.dumps(b))
        return "\n".join(out)
    return json.dumps(content)


def _truncate(text: str, limit: int) -> str:
    text = text.rstrip()
    return text if len(text) <= limit else text[:limit] + f" …(+{len(text) - limit} chars)"


def event_texts(evt: dict) -> list[str]:
    """Every text payload in one event, untruncated.

    The raw counterpart to render_event(), which trims payloads to keep the
    human log readable. Callers that parse the stream must read from here.
    """
    out: list[str] = []
    t = evt.get("type")
    if t == "assistant":
        for block in evt.get("message", {}).get("content", []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                out.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                # String values only: json.dumps() of the whole input would
                # escape any JSON nested in it (a Write's content) past parsing.
                inp = block.get("input", {})
                if isinstance(inp, dict):
                    out.extend(v for v in inp.values() if isinstance(v, str))
    elif t == "user":
        for block in evt.get("message", {}).get("content", []):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                out.append(tool_result_text(block.get("content")))
    elif t == "_invalid_json":
        # A marker can ride out on a line the stream-json decoder choked on.
        out.append(evt.get("raw", ""))
    return [x for x in out if isinstance(x, str) and x.strip()]


def render_event(evt: dict) -> str | None:
    """Turn one stream-json event into a human line (None = skip)."""
    t = evt.get("type")
    if t == "system":
        # Only the init frame is interesting (it names the model); later system
        # frames (thinking_tokens, etc.) are per-token noise — drop them.
        if evt.get("subtype") == "init":
            return f"[system:init] model={evt.get('model', '?')}"
        return None
    if t == "assistant":
        out = []
        for block in evt.get("message", {}).get("content", []):
            bt = block.get("type")
            if bt == "text":
                out.append(block.get("text", "").rstrip())
            elif bt == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input", {})
                if name == "Bash":
                    out.append(f"$ {inp.get('command', '')}")
                else:
                    out.append(f"[tool:{name}] {_truncate(json.dumps(inp), 200)}")
        return "\n".join(x for x in out if x) or None
    if t == "user":
        parts = []
        for block in evt.get("message", {}).get("content", []):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                parts.append("  → " + _truncate(tool_result_text(block.get("content")), 600))
        return "\n".join(parts) or None
    if t == "result":
        cost = evt.get("total_cost_usd")
        cost_s = f"${cost:.4f}" if isinstance(cost, (int, float)) else "?"
        return f"[result:{evt.get('subtype', '')}] cost={cost_s} turns={evt.get('num_turns', '?')}"
    if t == "_stderr":
        return f"[stderr] {evt.get('text', '').rstrip()}" if evt.get("text", "").strip() else None
    if t == "_invalid_json":
        return f"[raw] {evt.get('raw', '')}"
    return None

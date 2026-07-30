"""Turn analyzer stream events into short, readable log lines.

Both analyzer backends (sandbox and local CLI) stream one JSON event per
chunk (claude-code stream-json format). Each event becomes at most one
short line; events with nothing useful for a reader return None. Because
each event becomes one short line, a full run's log stays small and is
stored whole.
"""

from __future__ import annotations

import json

_SNIPPET_CHARS = 200


def _snippet(text: str) -> str:
    text = " ".join(str(text).split())
    if len(text) > _SNIPPET_CHARS:
        return text[: _SNIPPET_CHARS - 1] + "…"
    return text


def render_event_line(chunk: str) -> str | None:
    """Return one readable log line for a streamed event, or None to skip."""
    chunk = chunk.strip()
    if not chunk:
        return None
    try:
        event = json.loads(chunk)
    except (json.JSONDecodeError, ValueError):
        return _snippet(chunk)
    if not isinstance(event, dict):
        return _snippet(chunk)

    kind = event.get("type")
    if kind == "sandbox_info":
        sid = event.get("sandbox_id")
        return f"[sandbox] running in Daytona sandbox {sid}"
    if kind == "system":
        model = event.get("model") or ""
        return f"[start] session started{f' (model {model})' if model else ''}"
    if kind == "assistant":
        lines: list[str] = []
        content = (event.get("message") or {}).get("content") or []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and part.get("text", "").strip():
                lines.append(f"[assistant] {_snippet(part['text'])}")
            elif part.get("type") == "tool_use":
                name = part.get("name", "tool")
                arg = part.get("input") or {}
                # Show the one argument that identifies the call.
                detail = (
                    arg.get("file_path")
                    or arg.get("path")
                    or arg.get("pattern")
                    or arg.get("command")
                    or ""
                )
                lines.append(f"[tool] {name} {_snippet(str(detail))}".rstrip())
        return "\n".join(lines) if lines else None
    if kind == "result":
        err = event.get("is_error")
        return "[done] analyzer finished" + (" with an error" if err else "")
    # Tool results and other event types are noise at this zoom level.
    return None


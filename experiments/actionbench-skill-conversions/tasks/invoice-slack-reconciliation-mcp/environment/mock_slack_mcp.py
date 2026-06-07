#!/usr/bin/env python3
"""Mock Slack MCP (stdio) for the ActionBench skill-conversion experiment.

A single, self-contained stdio JSON-RPC MCP server that exposes one seeded Slack
channel through a small read-only tool surface. It is reused across tasks; the
channel data is loaded from the seed JSON passed as argv[1]. The seed has the
shape of a Slack export::

    {"channel": "<name>",
     "messages": [
        {"ts": "...", "user": "...", "text": "...",
         "attachments": [{"fallback": "...", "pretext": "...", "text": "..."}],
         "blocks": [{"type": "section", "text": "..."},
                    {"type": "context", "elements": [{"text": "..."}]}],
         "thread_replies": [{"ts": "...", "user": "...", "text": "..."}]}
     ]}

Tool surface (read-only)::

    slack_list_channels(query?)                 -> the seeded channel
    slack_read_channel(channel_id, cursor?,     -> one page of top-level messages
                       limit?)                     (reverse-chronological) with
                                                    attachments + blocks rendered
                                                    inline and a reply_count;
                                                    returns next_cursor for paging
    slack_read_thread(channel_id, thread_ts)    -> replies under a parent message
    slack_search_messages(query, limit?)        -> capped text search across
                                                    messages, attachments, blocks,
                                                    and thread replies

Why it is shaped this way
-------------------------
The agent must paginate the channel AND open threads to recover every message;
in the seeded data the correction tokens are deliberately scattered across
message text, attachment fallback/pretext/text, block text, and threaded
replies, so a single first-page read or a single capped search is not enough.
This mirrors the validated actionbench ``slack-support-metrics`` mock.

Sealing note: the raw seed is kept out of the agent workspace (under
``/opt/slack-mcp/seed``) and is never referenced in the prompt. The harbor
skills environment runs the agent as root, so file permissions cannot fully
seal the seed; production hardening (a root-owned seed daemon behind a localhost
socket plus a non-root agent user, exactly as in ``slack-support-metrics``) is
described in the experiment README. The spec deprioritizes adversarial sealing,
so this conversion ships the soft seal and documents the hardening.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

PAGE_SIZE_DEFAULT = 6
SEARCH_CAP_DEFAULT = 20


def load_seed() -> dict[str, Any]:
    if len(sys.argv) < 2:
        raise SystemExit("usage: mock_slack_mcp.py <seed.json> [server_name]")
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    messages = list(data.get("messages", []))
    # Reverse-chronological (newest first), like a real Slack channel read.
    # Seed timestamps are either ISO-8601 ('Z') or zero-padded epoch floats;
    # both sort chronologically as plain strings.
    messages.sort(key=lambda m: str(m.get("ts", "")), reverse=True)
    data["messages"] = messages
    return data


SEED = load_seed()
SERVER_NAME = sys.argv[2] if len(sys.argv) > 2 else "slack"
CHANNEL_NAME = str(SEED.get("channel", "channel"))
CHANNEL_ID = "C_" + "".join(ch for ch in CHANNEL_NAME.upper() if ch.isalnum())[:16]


def channel_matches(channel_id: str) -> bool:
    cid = str(channel_id or "").lstrip("#")
    return cid in {CHANNEL_ID, CHANNEL_NAME}


def render_blocks(blocks: Any) -> list[str]:
    out: list[str] = []
    if not isinstance(blocks, list):
        return out
    for block in blocks:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str):
            out.append(text)
        elif isinstance(text, dict) and isinstance(text.get("text"), str):
            out.append(text["text"])
        for element in block.get("elements", []) or []:
            if isinstance(element, dict) and isinstance(element.get("text"), str):
                out.append(element["text"])
    return out


def render_attachments(attachments: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not isinstance(attachments, list):
        return out
    for att in attachments:
        if not isinstance(att, dict):
            continue
        rendered = {k: att[k] for k in ("pretext", "fallback", "text", "title") if isinstance(att.get(k), str)}
        if rendered:
            out.append(rendered)
    return out


def render_message(message: dict[str, Any], include_thread: bool = False) -> dict[str, Any]:
    replies = message.get("thread_replies") or []
    rendered: dict[str, Any] = {
        "ts": message.get("ts", ""),
        "user": message.get("user", ""),
        "text": message.get("text", ""),
        "reply_count": len(replies),
    }
    attachments = render_attachments(message.get("attachments"))
    if attachments:
        rendered["attachments"] = attachments
    blocks = render_blocks(message.get("blocks"))
    if blocks:
        rendered["blocks"] = blocks
    if replies:
        rendered["thread_ts"] = message.get("ts", "")
        rendered["note"] = "This message has thread replies — call slack_read_thread(thread_ts) to read them."
    if include_thread:
        rendered["replies"] = [
            {"ts": r.get("ts", ""), "user": r.get("user", ""), "text": r.get("text", "")}
            for r in replies
            if isinstance(r, dict)
        ]
    return rendered


# --- tool implementations -------------------------------------------------

def slack_list_channels(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "channels": [
            {"id": CHANNEL_ID, "name": CHANNEL_NAME, "is_member": True, "num_messages": len(SEED["messages"])}
        ]
    }


def slack_read_channel(args: dict[str, Any]) -> dict[str, Any]:
    channel_id = str(args.get("channel_id", ""))
    if channel_id and not channel_matches(channel_id):
        return {"error": f"channel_not_found: {channel_id}", "hint": "call slack_list_channels first"}
    try:
        limit = int(args.get("limit") or PAGE_SIZE_DEFAULT)
    except (TypeError, ValueError):
        limit = PAGE_SIZE_DEFAULT
    limit = max(1, min(limit, 50))
    try:
        offset = int(args.get("cursor") or 0)
    except (TypeError, ValueError):
        return {"error": f"invalid cursor: {args.get('cursor')!r}"}
    messages = SEED["messages"]
    page = messages[offset : offset + limit]
    next_offset = offset + limit
    has_more = next_offset < len(messages)
    return {
        "channel": CHANNEL_NAME,
        "channel_id": CHANNEL_ID,
        "messages": [render_message(m) for m in page],
        "returned": len(page),
        "total_messages": len(messages),
        "has_more": has_more,
        "next_cursor": str(next_offset) if has_more else None,
        "note": (
            "Reverse-chronological. Pass next_cursor back as 'cursor' to read the next page. "
            "Some messages carry corrections inside attachments, blocks, or thread replies."
        ),
    }


def slack_read_thread(args: dict[str, Any]) -> dict[str, Any]:
    channel_id = str(args.get("channel_id", ""))
    if channel_id and not channel_matches(channel_id):
        return {"error": f"channel_not_found: {channel_id}"}
    thread_ts = str(args.get("thread_ts") or args.get("message_ts") or "")
    for message in SEED["messages"]:
        if str(message.get("ts", "")) == thread_ts:
            return {
                "channel_id": CHANNEL_ID,
                "thread_ts": thread_ts,
                "parent": render_message(message),
                "replies": render_message(message, include_thread=True).get("replies", []),
            }
    return {"error": f"thread_not_found: {thread_ts}"}


def _iter_text_surfaces():
    """Yield (ts, location, text) across top-level message surfaces only.

    Thread replies are intentionally NOT indexed here: like a real Slack search,
    slack_search_messages surfaces channel messages (and their attachments and
    blocks), and replies must be read with slack_read_thread. This keeps thread
    reads necessary rather than letting one search shortcut the whole channel.
    """
    for message in SEED["messages"]:
        ts = str(message.get("ts", ""))
        if isinstance(message.get("text"), str):
            yield ts, "message", message["text"]
        for att in render_attachments(message.get("attachments")):
            for value in att.values():
                yield ts, "attachment", value
        for block_text in render_blocks(message.get("blocks")):
            yield ts, "block", block_text


def slack_search_messages(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query", "")).strip().lower()
    try:
        limit = int(args.get("limit") or SEARCH_CAP_DEFAULT)
    except (TypeError, ValueError):
        limit = SEARCH_CAP_DEFAULT
    limit = max(1, min(limit, SEARCH_CAP_DEFAULT))
    matches = []
    for ts, location, text in _iter_text_surfaces():
        if not query or query in text.lower():
            matches.append({"ts": ts, "channel_id": CHANNEL_ID, "location": location, "text": text})
    capped = matches[:limit]
    return {
        "query": args.get("query", ""),
        "matches": capped,
        "returned": len(capped),
        "total_matches": len(matches),
        "capped": len(matches) > len(capped),
        "note": (
            "Search is capped and does NOT include thread replies. For complete coverage, paginate "
            "the channel with slack_read_channel and open every thread with slack_read_thread."
        ),
    }


TOOLS = [
    {
        "name": "slack_list_channels",
        "description": "List the Slack channels the current user can read.",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
    {
        "name": "slack_read_channel",
        "description": (
            "Read one page of a channel's top-level messages in reverse-chronological order. "
            "Attachments and blocks are included inline. Messages with replies show reply_count and "
            "a thread_ts; use slack_read_thread to read the replies. Use next_cursor to paginate."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel_id": {"type": "string"},
                "cursor": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["channel_id"],
        },
    },
    {
        "name": "slack_read_thread",
        "description": "Read the parent message and all replies of a thread, identified by its thread_ts.",
        "inputSchema": {
            "type": "object",
            "properties": {"channel_id": {"type": "string"}, "thread_ts": {"type": "string"}},
            "required": ["channel_id", "thread_ts"],
        },
    },
    {
        "name": "slack_search_messages",
        "description": (
            "Search message/attachment/block/reply text for a substring. Results are CAPPED and not "
            "exhaustive; for complete coverage paginate the channel and open threads."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"],
        },
    },
]

DISPATCH = {
    "slack_list_channels": slack_list_channels,
    "slack_read_channel": slack_read_channel,
    "slack_read_thread": slack_read_thread,
    "slack_search_messages": slack_search_messages,
}


def text_result(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2, sort_keys=True)}]}


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    handler = DISPATCH.get(name)
    if handler is None:
        return {"content": [{"type": "text", "text": json.dumps({"error": f"unknown tool: {name}"})}], "isError": True}
    try:
        return text_result(handler(arguments or {}))
    except Exception as exc:  # never crash the agent's MCP session
        return {"content": [{"type": "text", "text": json.dumps({"error": str(exc)})}], "isError": True}


# --- stdio JSON-RPC transport (supports framed and line-delimited) --------

def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    msg_id = message.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": "0.1.0"},
            },
        }
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = message.get("params", {}) or {}
        result = call_tool(str(params.get("name", "")), params.get("arguments") or {})
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def read_messages():
    buf = sys.stdin.buffer
    while True:
        line = buf.readline()
        if not line:
            return
        if line.startswith(b"Content-Length:"):
            length = int(line.split(b":", 1)[1].strip())
            while True:
                blank = buf.readline()
                if blank in (b"\r\n", b"\n", b""):
                    break
            payload = buf.read(length)
            yield json.loads(payload.decode("utf-8")), True
        elif line.strip():
            yield json.loads(line.decode("utf-8")), False


def send(message: dict[str, Any], framed: bool) -> None:
    data = json.dumps(message, separators=(",", ":")).encode("utf-8")
    out = sys.stdout.buffer
    if framed:
        out.write(b"Content-Length: " + str(len(data)).encode("ascii") + b"\r\n\r\n")
    out.write(data if framed else data + b"\n")
    out.flush()


def main() -> None:
    for message, framed in read_messages():
        response = handle(message)
        if response is not None:
            send(response, framed)


if __name__ == "__main__":
    main()

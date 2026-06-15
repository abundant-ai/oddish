from __future__ import annotations

import json
from typing import Any

import logging

log = logging.getLogger("oddish.cc_chat.transcript_buffer")


class SessionTranscriptBuffer:
    """Per-session in-memory buffer of stream-json events.

    Events are appended as the orchestrator emits them. Flushed to MinIO
    once at close via `to_jsonl(session_id)`. Lost on service crash by
    design — see spec section "Service-restart recovery"."""

    def __init__(self) -> None:
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._size_log_threshold = 1000

    def append(self, session_id: str, event: dict[str, Any]) -> None:
        events = self._events.setdefault(session_id, [])
        events.append(event)
        # Heartbeat at every 1k events so unbounded growth is observable.
        if len(events) % self._size_log_threshold == 0:
            log.info(
                "transcript_buffer.size_milestone",
                session_id=session_id,
                count=len(events),
            )

    def events(self, session_id: str) -> list[dict[str, Any]]:
        return list(self._events.get(session_id, []))

    def to_jsonl(self, session_id: str) -> bytes:
        events = self._events.get(session_id, [])
        return ("\n".join(json.dumps(e) for e in events) + "\n").encode("utf-8") if events else b""

    def drop(self, session_id: str) -> None:
        self._events.pop(session_id, None)

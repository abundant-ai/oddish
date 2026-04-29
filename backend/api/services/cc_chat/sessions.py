from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterator


@dataclass
class SessionState:
    session_id: str
    experiment_id: str
    org_id: str
    sandbox_id: str
    daytona_session_id: str
    created_at: datetime
    last_activity: datetime
    claude_session_id: str | None
    broken: bool = False


class SessionRegistry:
    """In-memory session map. Single-replica only; documented limitation."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}

    def put(self, state: SessionState) -> None:
        self._sessions[state.session_id] = state

    def get(self, session_id: str) -> SessionState | None:
        return self._sessions.get(session_id)

    def pop(self, session_id: str) -> SessionState | None:
        return self._sessions.pop(session_id, None)

    def touch(self, session_id: str, *, now: datetime) -> None:
        state = self._sessions.get(session_id)
        if state is not None:
            state.last_activity = now

    def idle(
        self, *, now: datetime, max_idle: timedelta
    ) -> Iterator[SessionState]:
        cutoff = now - max_idle
        for state in list(self._sessions.values()):
            if state.last_activity < cutoff:
                yield state

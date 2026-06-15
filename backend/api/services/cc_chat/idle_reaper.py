from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Callable, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession

from models import ChatSession, ChatStatus

log = logging.getLogger("oddish.cc_chat.idle_reaper")


class _Closeable(Protocol):
    async def close(self, *, session_id: str, db_session_factory): ...


async def reap_idle_sessions(
    *,
    orchestrator: _Closeable,
    idle_threshold_minutes: int,
    db_session_factory: Callable[[], object],
) -> int:
    threshold = datetime.utcnow() - timedelta(minutes=idle_threshold_minutes)
    sess = db_session_factory()
    try:
        if isinstance(sess, _AsyncSession):
            ids = await _query_ids(sess, threshold)
        else:
            async with sess as s:
                ids = await _query_ids(s, threshold)
    except Exception:
        log.exception("reap_idle_sessions: query failed")
        return 0

    for sid in ids:
        try:
            await orchestrator.close(
                session_id=sid, db_session_factory=db_session_factory
            )
        except Exception:
            log.exception("reap_idle_sessions: close failed session_id=%s", sid)
    return len(ids)


async def _query_ids(session, threshold: datetime) -> list[str]:
    stmt = select(ChatSession.id).where(
        ChatSession.status == ChatStatus.active.value,
        ChatSession.last_activity < threshold,
    )
    return list((await session.execute(stmt)).scalars().all())


async def run_reaper_loop(
    *,
    orchestrator: _Closeable,
    idle_threshold_minutes: int,
    interval_seconds: int,
    db_session_factory: Callable[[], object],
    stop_event: asyncio.Event,
) -> None:
    """Background task: every interval, sweep idle sessions. Exits when stop_event is set."""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            return  # stop_event set during sleep
        except asyncio.TimeoutError:
            pass  # tick

        try:
            n = await reap_idle_sessions(
                orchestrator=orchestrator,
                idle_threshold_minutes=idle_threshold_minutes,
                db_session_factory=db_session_factory,
            )
            if n:
                log.info("idle_reaper.swept count=%d", n)
        except Exception:
            log.exception("idle_reaper.tick_failed")

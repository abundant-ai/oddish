from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession

from api.services.cc_chat.daytona_client import CreatedSandbox, DaytonaClient
from models import ChatSession, ChatStatus, ChatTurn
from oddish.db.models import utcnow

log = logging.getLogger("oddish.cc_chat.restart_sweep")


async def sweep_orphan_chat_sessions(
    *,
    daytona: DaytonaClient,
    db_session_factory: Callable[[], object],
) -> int:
    sess = db_session_factory()
    try:
        if isinstance(sess, _AsyncSession):
            return await _do_sweep(daytona, sess)
        async with sess as s:
            return await _do_sweep(daytona, s)
    except Exception:
        log.exception("chat.restart_sweep failed")
        return 0


async def _do_sweep(daytona: DaytonaClient, session) -> int:
    stmt = select(ChatSession).where(
        ChatSession.status.in_([ChatStatus.active.value, ChatStatus.provisioning.value])
    )
    rows = (await session.execute(stmt)).scalars().all()
    now = utcnow()
    for row in rows:
        row.status = ChatStatus.broken.value
        row.error = "service restart"
        row.closed_at = now
        if row.sandbox_id:
            await _quiet_delete(daytona, row.sandbox_id)
    await session.commit()

    # Mark any running turns as failed; preserve event log.
    await session.execute(
        update(ChatTurn).where(ChatTurn.status == "running").values(status="failed", error="service restart")
    )
    await session.commit()

    return len(rows)


async def _quiet_delete(daytona: DaytonaClient, sandbox_id: str) -> None:
    try:
        await daytona.delete_sandbox(
            CreatedSandbox(id=sandbox_id, _sdk_handle=sandbox_id)
        )
    except Exception:
        log.warning("chat.restart_sweep delete failed sandbox_id=%s", sandbox_id)

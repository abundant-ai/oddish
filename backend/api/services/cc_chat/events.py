from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import ChatSessionEvent, generate_id


async def append_event(session: AsyncSession, *, session_id: str, event: dict) -> int:
    """Append one stream-json event; returns the assigned monotonic seq.

    Safe because the one-running-turn invariant serializes a session's writers,
    so max(seq)+1 has no concurrent contender. The unique (session_id, seq)
    constraint is a backstop that surfaces any violation as an error.
    """
    next_seq = (
        await session.execute(
            select(func.coalesce(func.max(ChatSessionEvent.seq), -1)).where(
                ChatSessionEvent.session_id == session_id
            )
        )
    ).scalar_one() + 1
    session.add(
        ChatSessionEvent(id=generate_id(), session_id=session_id, seq=next_seq, event=event)
    )
    await session.flush()
    return next_seq


async def read_events(session: AsyncSession, *, session_id: str, since: int = -1) -> list[dict]:
    rows = (
        await session.execute(
            select(ChatSessionEvent.seq, ChatSessionEvent.event)
            .where(ChatSessionEvent.session_id == session_id, ChatSessionEvent.seq > since)
            .order_by(ChatSessionEvent.seq.asc())
        )
    ).all()
    return [{"seq": seq, "event": ev} for seq, ev in rows]


async def prune_events(session: AsyncSession, *, session_id: str) -> int:
    result = await session.execute(
        delete(ChatSessionEvent).where(ChatSessionEvent.session_id == session_id)
    )
    return result.rowcount or 0

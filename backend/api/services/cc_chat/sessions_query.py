from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import ChatSession, ChatTurn


async def list_sessions(
    session: AsyncSession,
    *,
    org_id: str,
    scope_kind: str,
    scope_id: str,
    limit: int,
    offset: int,
    q: str | None,
) -> tuple[list[dict], int]:
    """Return (sessions, total) for an org+scope, most-recently-active first.
    ``q`` does a case-insensitive title contains-match."""
    where = [
        ChatSession.org_id == org_id,
        ChatSession.scope_kind == scope_kind,
        ChatSession.scope_id == scope_id,
    ]
    if q:
        where.append(ChatSession.title.ilike(f"%{q}%"))

    total = (
        await session.execute(select(func.count()).select_from(ChatSession).where(*where))
    ).scalar_one()

    turn_count = (
        select(func.count())
        .select_from(ChatTurn)
        .where(ChatTurn.session_id == ChatSession.id)
        .scalar_subquery()
    )
    rows = (
        await session.execute(
            select(
                ChatSession.id,
                ChatSession.title,
                ChatSession.status,
                ChatSession.created_at,
                ChatSession.last_activity,
                turn_count.label("turn_count"),
            )
            .where(*where)
            .order_by(ChatSession.last_activity.desc())
            .limit(min(limit, 50))
            .offset(offset)
        )
    ).all()

    items = [
        {
            "id": r.id,
            "title": r.title,
            "status": r.status,
            "created_at": r.created_at,
            "last_activity": r.last_activity,
            "turn_count": r.turn_count,
        }
        for r in rows
    ]
    return items, total

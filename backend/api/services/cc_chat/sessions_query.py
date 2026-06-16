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
    if q and q.strip():
        where.append(ChatSession.title.ilike(f"%{q.strip()}%"))

    total = (
        await session.execute(select(func.count()).select_from(ChatSession).where(*where))
    ).scalar_one()

    # Page the sessions first (served by ix_chat_sessions_org_scope_activity),
    # then fetch turn counts for just this page in one grouped query. This
    # avoids a per-row correlated subquery, which degraded badly under load.
    rows = (
        await session.execute(
            select(
                ChatSession.id,
                ChatSession.title,
                ChatSession.status,
                ChatSession.created_at,
                ChatSession.last_activity,
            )
            .where(*where)
            .order_by(ChatSession.last_activity.desc())
            .limit(min(limit, 50))
            .offset(offset)
        )
    ).all()

    page_ids = [r.id for r in rows]
    counts: dict[str, int] = {}
    if page_ids:
        count_rows = (
            await session.execute(
                select(ChatTurn.session_id, func.count())
                .where(ChatTurn.session_id.in_(page_ids))
                .group_by(ChatTurn.session_id)
            )
        ).all()
        counts = {sid: n for sid, n in count_rows}

    items = [
        {
            "id": r.id,
            "title": r.title,
            "status": r.status,
            "created_at": r.created_at,
            "last_activity": r.last_activity,
            "turn_count": counts.get(r.id, 0),
        }
        for r in rows
    ]
    return items, total

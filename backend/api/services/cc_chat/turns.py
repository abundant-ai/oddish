from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import ChatSession, ChatTurn, generate_id
from oddish.db.models import utcnow

_TITLE_MAX = 80


def _title_from(message: str) -> str:
    one_line = " ".join(message.split())
    return one_line[:_TITLE_MAX]


async def open_turn(session: AsyncSession, *, session_id: str, user_message: str) -> ChatTurn:
    """Open a running turn. On the first turn, sets the session title from the
    user message (truncated to one line). Raises IntegrityError if a turn is
    already running (partial unique index uq_chat_turns_one_running)."""
    next_seq = (
        await session.execute(
            select(func.coalesce(func.max(ChatTurn.seq), -1)).where(ChatTurn.session_id == session_id)
        )
    ).scalar_one() + 1
    if next_seq == 0:
        chat = await session.get(ChatSession, session_id)
        if chat is not None and not chat.title:
            chat.title = _title_from(user_message)
    turn = ChatTurn(
        id=generate_id(), session_id=session_id, seq=next_seq,
        user_message=user_message, status="running", started_at=utcnow(),
    )
    session.add(turn)
    await session.flush()
    return turn


async def close_turn(session: AsyncSession, *, turn_id: str, status: str, error: str | None = None) -> None:
    turn = await session.get(ChatTurn, turn_id)
    if turn is not None:
        turn.status = status
        turn.ended_at = utcnow()
        turn.error = error
        await session.flush()


async def running_turn(session: AsyncSession, *, session_id: str) -> ChatTurn | None:
    return (
        await session.execute(
            select(ChatTurn).where(ChatTurn.session_id == session_id, ChatTurn.status == "running")
        )
    ).scalar_one_or_none()

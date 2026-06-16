import pytest
from tests.cc_chat.conftest import seed_session
from api.services.cc_chat.turns import open_turn
from models import ChatSession

pytestmark = pytest.mark.asyncio


async def test_first_turn_sets_title_subsequent_turns_do_not(db):
    await seed_session(db, status="active")

    async with db() as s:
        await open_turn(s, session_id="cs_1", user_message="Why did trial_f fail on a long input " + "x" * 200)
        await s.commit()
    async with db() as s:
        row = await s.get(ChatSession, "cs_1")
        assert row.title is not None
        assert row.title.startswith("Why did trial_f fail")
        assert len(row.title) <= 80
        first_title = row.title

    async with db() as s:
        from api.services.cc_chat.turns import close_turn, running_turn
        running = await running_turn(s, session_id="cs_1")
        await close_turn(s, turn_id=running.id, status="done")
        await open_turn(s, session_id="cs_1", user_message="a totally different second question")
        await s.commit()
    async with db() as s:
        row = await s.get(ChatSession, "cs_1")
        assert row.title == first_title  # unchanged

import pytest
from sqlalchemy.exc import IntegrityError
from tests.cc_chat.conftest import seed_session
from api.services.cc_chat import turns

pytestmark = pytest.mark.asyncio


async def test_open_then_close_turn(db):
    await seed_session(db)
    async with db() as s:
        t = await turns.open_turn(s, session_id="cs_1", user_message="hi")
        await s.commit()
        assert t.seq == 0 and t.status == "running"
    async with db() as s:
        await turns.close_turn(s, turn_id=t.id, status="done")
        await s.commit()
    async with db() as s:
        assert await turns.running_turn(s, session_id="cs_1") is None


async def test_second_running_turn_is_rejected(db):
    await seed_session(db)
    async with db() as s:
        await turns.open_turn(s, session_id="cs_1", user_message="first")
        await s.commit()
    with pytest.raises(IntegrityError):
        async with db() as s:
            await turns.open_turn(s, session_id="cs_1", user_message="second")
            await s.commit()


async def test_running_turn_returns_open_turn(db):
    await seed_session(db)
    async with db() as s:
        t = await turns.open_turn(s, session_id="cs_1", user_message="hi")
        await s.commit()
    async with db() as s:
        found = await turns.running_turn(s, session_id="cs_1")
    assert found is not None and found.id == t.id

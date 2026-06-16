import pytest
from tests.cc_chat.conftest import seed_session
from api.services.cc_chat import events

pytestmark = pytest.mark.asyncio


async def test_append_assigns_monotonic_seq_and_reads_back(db):
    await seed_session(db)
    async with db() as s:
        assert await events.append_event(s, session_id="cs_1", event={"type": "a"}) == 0
        assert await events.append_event(s, session_id="cs_1", event={"type": "b"}) == 1
        await s.commit()
    async with db() as s:
        rows = await events.read_events(s, session_id="cs_1")
    assert [r["seq"] for r in rows] == [0, 1]
    assert rows[0]["event"] == {"type": "a"}


async def test_read_since_filters(db):
    await seed_session(db)
    async with db() as s:
        for i in range(3):
            await events.append_event(s, session_id="cs_1", event={"i": i})
        await s.commit()
    async with db() as s:
        rows = await events.read_events(s, session_id="cs_1", since=0)
    assert [r["seq"] for r in rows] == [1, 2]


async def test_prune_deletes_all_events_for_session(db):
    await seed_session(db)
    async with db() as s:
        await events.append_event(s, session_id="cs_1", event={"x": 1})
        await s.commit()
    async with db() as s:
        deleted = await events.prune_events(s, session_id="cs_1")
        await s.commit()
    assert deleted == 1
    async with db() as s:
        assert await events.read_events(s, session_id="cs_1") == []

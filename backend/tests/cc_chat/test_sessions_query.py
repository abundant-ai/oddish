import pytest
from tests.cc_chat.conftest import seed_session, ORG
from api.services.cc_chat.sessions_query import list_sessions
from models import ChatSession

pytestmark = pytest.mark.asyncio


async def _set_title(db, sid, title):
    async with db() as s:
        row = await s.get(ChatSession, sid)
        row.title = title
        await s.commit()


async def test_list_filters_by_scope_and_searches_title(db):
    await seed_session(db, session_id="cs_a", scope_kind="experiment", scope_id="exp_1")
    await seed_session(db, session_id="cs_b", scope_kind="experiment", scope_id="exp_1")
    await seed_session(db, session_id="cs_c", scope_kind="experiment", scope_id="exp_other")
    await _set_title(db, "cs_a", "timeout investigation")
    await _set_title(db, "cs_b", "scoring rubric question")

    async with db() as s:
        items, total = await list_sessions(
            s, org_id=ORG, scope_kind="experiment", scope_id="exp_1", limit=10, offset=0, q=None
        )
    ids = {i["id"] for i in items}
    assert ids == {"cs_a", "cs_b"}      # exp_other excluded
    assert total == 2

    async with db() as s:
        items, total = await list_sessions(
            s, org_id=ORG, scope_kind="experiment", scope_id="exp_1", limit=10, offset=0, q="timeout"
        )
    assert [i["id"] for i in items] == ["cs_a"]
    assert total == 1


async def test_list_excludes_other_orgs(db):
    await seed_session(db, session_id="cs_a", scope_kind="experiment", scope_id="exp_1")
    async with db() as s:
        items, total = await list_sessions(
            s, org_id="org_someone_else", scope_kind="experiment", scope_id="exp_1",
            limit=10, offset=0, q=None,
        )
    assert items == [] and total == 0


async def test_turn_count_and_ordering(db):
    from datetime import datetime, timezone
    from models import ChatTurn, generate_id

    await seed_session(db, session_id="cs_old", scope_kind="experiment", scope_id="exp_1")
    await seed_session(db, session_id="cs_new", scope_kind="experiment", scope_id="exp_1")

    # distinct last_activity: cs_new is more recent than cs_old
    async with db() as s:
        old = await s.get(ChatSession, "cs_old")
        new = await s.get(ChatSession, "cs_new")
        old.last_activity = datetime(2026, 1, 1, tzinfo=timezone.utc)
        new.last_activity = datetime(2026, 6, 1, tzinfo=timezone.utc)
        # two turns on cs_old, zero on cs_new
        s.add(ChatTurn(id=generate_id(), session_id="cs_old", seq=0, user_message="a", status="done"))
        s.add(ChatTurn(id=generate_id(), session_id="cs_old", seq=1, user_message="b", status="done"))
        await s.commit()

    async with db() as s:
        items, total = await list_sessions(
            s, org_id=ORG, scope_kind="experiment", scope_id="exp_1", limit=10, offset=0, q=None
        )
    assert total == 2
    # most-recently-active first
    assert [i["id"] for i in items] == ["cs_new", "cs_old"]
    by_id = {i["id"]: i for i in items}
    assert by_id["cs_old"]["turn_count"] == 2
    assert by_id["cs_new"]["turn_count"] == 0


async def test_limit_is_capped_at_50(db):
    # seed 55 sessions; even with limit=100 we must get at most 50
    for n in range(55):
        await seed_session(db, session_id=f"cs_{n:03d}", scope_kind="experiment", scope_id="exp_cap")
    async with db() as s:
        items, total = await list_sessions(
            s, org_id=ORG, scope_kind="experiment", scope_id="exp_cap", limit=100, offset=0, q=None
        )
    assert total == 55
    assert len(items) == 50


async def test_whitespace_query_is_ignored(db):
    await seed_session(db, session_id="cs_w", scope_kind="experiment", scope_id="exp_ws")
    async with db() as s:
        items, total = await list_sessions(
            s, org_id=ORG, scope_kind="experiment", scope_id="exp_ws", limit=10, offset=0, q="   "
        )
    assert total == 1 and len(items) == 1

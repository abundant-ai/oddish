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

import pytest
from tests.cc_chat.conftest import seed_session
from api.services.cc_chat import events, turns, restart_sweep

pytestmark = pytest.mark.asyncio


class _FakeDaytona:
    async def delete_sandbox(self, sandbox):
        return None


async def test_sweep_fails_running_turn_but_preserves_events(db):
    await seed_session(db, status="active")
    async with db() as s:
        await turns.open_turn(s, session_id="cs_1", user_message="hi")
        await events.append_event(s, session_id="cs_1", event={"type": "assistant"})
        await s.commit()

    def _factory():
        return db()

    swept = await restart_sweep.sweep_orphan_chat_sessions(daytona=_FakeDaytona(), db_session_factory=_factory)
    assert swept >= 1

    async with db() as s:
        assert await turns.running_turn(s, session_id="cs_1") is None       # turn failed
        rows = await events.read_events(s, session_id="cs_1")
    assert len(rows) == 1                                                    # events preserved

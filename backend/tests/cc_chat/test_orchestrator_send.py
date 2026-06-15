import pytest
from contextlib import asynccontextmanager
from tests.cc_chat.conftest import seed_session
from api.services.cc_chat import events as events_mod, turns as turns_mod
from api.services.cc_chat.orchestrator import ChatOrchestrator
from api.services.cc_chat.transcript_buffer import SessionTranscriptBuffer

pytestmark = pytest.mark.asyncio


class _FakeRuntime:
    async def stream_chat(self, daytona, sandbox, *, content, claude_session_id, daytona_session_id):
        yield {"type": "system", "subtype": "init", "session_id": "claude-xyz"}
        yield {"type": "assistant", "text": "hello"}


class _FakeSandbox:
    id = "sbx_1"


async def test_send_persists_events_and_tracks_turn(db):
    await seed_session(db, status="active")

    def factory():
        @asynccontextmanager
        async def _cm():
            async with db() as s:
                yield s
        return _cm()

    orch = ChatOrchestrator(
        daytona=object(),
        runtime=_FakeRuntime(),
        transcript_buffer=SessionTranscriptBuffer(),
        anthropic_api_key="test",
    )
    orch._sandboxes["cs_1"] = _FakeSandbox()

    seen = []
    async for ev in orch.send(session_id="cs_1", content="hi", db_session_factory=factory):
        seen.append(ev)

    assert len(seen) == 2

    async with db() as s:
        rows = await events_mod.read_events(s, session_id="cs_1")
    assert [r["event"]["type"] for r in rows] == ["system", "assistant"]

    async with db() as s:
        from models import ChatSession
        row = await s.get(ChatSession, "cs_1")
        assert row.claude_session_id == "claude-xyz"

    async with db() as s:
        assert await turns_mod.running_turn(s, session_id="cs_1") is None

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


async def test_send_marks_session_broken_when_sandbox_gone(db):
    from daytona import DaytonaNotFoundError
    from models import ChatSession, ChatStatus
    await seed_session(db, status="active")

    def factory():
        @asynccontextmanager
        async def _cm():
            async with db() as s:
                yield s
        return _cm()

    class _GoneRuntime:
        async def stream_chat(self, daytona, sandbox, *, content, claude_session_id, daytona_session_id):
            raise DaytonaNotFoundError("sandbox not found")
            yield  # pragma: no cover  (forces this to be an async generator)

    orch = ChatOrchestrator(
        daytona=object(),
        runtime=_GoneRuntime(),
        transcript_buffer=SessionTranscriptBuffer(),
        anthropic_api_key="test",
    )
    orch._sandboxes["cs_1"] = _FakeSandbox()

    async for _ in orch.send(session_id="cs_1", content="hi", db_session_factory=factory):
        pass

    assert "cs_1" not in orch._sandboxes
    async with db() as s:
        row = await s.get(ChatSession, "cs_1")
    assert row.status == ChatStatus.broken.value


async def test_send_disconnect_closes_turn_as_canceled(db):
    await seed_session(db, status="active")

    def factory():
        from contextlib import asynccontextmanager
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

    agen = orch.send(session_id="cs_1", content="hi", db_session_factory=factory)
    await agen.__anext__()      # consume first event; generator suspended at yield
    await agen.aclose()         # simulate client disconnect (throws GeneratorExit)

    from api.services.cc_chat import turns as turns_mod
    from models import ChatTurn
    from sqlalchemy import select
    async with db() as s:
        assert await turns_mod.running_turn(s, session_id="cs_1") is None
        t = (await s.execute(select(ChatTurn).where(ChatTurn.session_id == "cs_1"))).scalar_one()
        assert t.status == "canceled"


async def test_send_reconnects_sandbox_on_a_different_container(db):
    """The API autoscales across containers with no session affinity, so the
    container handling a message usually isn't the one that ran start() and
    holds the sandbox handle in memory. send() must rehydrate the handle from
    the persisted sandbox_id instead of raising SessionNotFound."""
    from api.services.cc_chat.daytona_client import FakeDaytonaClient

    # Shared "remote" Daytona service + a sandbox created by container A.
    client = FakeDaytonaClient()
    sbx = await client.create_sandbox(
        env_vars={}, auto_stop_minutes=30, auto_delete_minutes=60, labels={}
    )
    await seed_session(db, status="active", sandbox_id=sbx.id)

    def factory():
        @asynccontextmanager
        async def _cm():
            async with db() as s:
                yield s
        return _cm()

    # Container B: fresh orchestrator, empty _sandboxes — never ran start().
    orch = ChatOrchestrator(
        daytona=client,
        runtime=_FakeRuntime(),
        transcript_buffer=SessionTranscriptBuffer(),
        anthropic_api_key="test",
    )
    assert "cs_1" not in orch._sandboxes

    seen = []
    async for ev in orch.send(session_id="cs_1", content="hi", db_session_factory=factory):
        seen.append(ev)

    assert len(seen) == 2
    # Handle was rehydrated by reconnecting to the existing sandbox.
    assert orch._sandboxes["cs_1"].id == sbx.id


async def test_send_session_not_found_when_sandbox_deleted(db):
    """If the persisted sandbox no longer exists (ephemeral, auto-stopped and
    evicted), reconnect fails and the session reads as not found."""
    from api.services.cc_chat.daytona_client import FakeDaytonaClient

    client = FakeDaytonaClient()
    sbx = await client.create_sandbox(
        env_vars={}, auto_stop_minutes=30, auto_delete_minutes=60, labels={}
    )
    client.deleted.add(sbx.id)
    await seed_session(db, status="active", sandbox_id=sbx.id)

    def factory():
        @asynccontextmanager
        async def _cm():
            async with db() as s:
                yield s
        return _cm()

    orch = ChatOrchestrator(
        daytona=client,
        runtime=_FakeRuntime(),
        transcript_buffer=SessionTranscriptBuffer(),
        anthropic_api_key="test",
    )

    from api.services.cc_chat.orchestrator import SessionNotFound
    with pytest.raises(SessionNotFound):
        async for _ in orch.send(session_id="cs_1", content="hi", db_session_factory=factory):
            pass


async def test_send_self_heals_evicted_sandbox_via_resume(db):
    """An idle chat loses its ephemeral sandbox to Daytona's auto-stop. The next
    message must transparently resume (re-provision + restore from the per-turn
    archive) instead of dead-ending the user with session_not_found."""
    from daytona import DaytonaNotFoundError
    from models import ChatSession

    await seed_session(db, status="active", sandbox_id="sbx_old")
    async with db() as s:
        row = await s.get(ChatSession, "cs_1")
        row.claude_session_id = "claude-xyz"  # has an archive -> recoverable
        await s.commit()

    def factory():
        @asynccontextmanager
        async def _cm():
            async with db() as s:
                yield s
        return _cm()

    class _Evicted:
        async def connect_sandbox(self, *, sandbox_id):
            raise DaytonaNotFoundError(f"gone: {sandbox_id}")

    orch = ChatOrchestrator(
        daytona=_Evicted(),
        runtime=_FakeRuntime(),
        transcript_buffer=SessionTranscriptBuffer(),
        anthropic_api_key="test",
    )

    resumed = {"called": False}

    async def _fake_resume(*, session_id, db_session_factory):
        resumed["called"] = True
        orch._sandboxes[session_id] = _FakeSandbox()

    orch.resume = _fake_resume

    seen = []
    async for ev in orch.send(session_id="cs_1", content="hi", db_session_factory=factory):
        seen.append(ev)

    assert resumed["called"]
    assert len(seen) == 2


async def test_send_archives_native_session_after_turn(db):
    await seed_session(db, status="active")

    def factory():
        @asynccontextmanager
        async def _cm():
            async with db() as s:
                yield s
        return _cm()

    class _Blob:
        def __init__(self): self.store = {}
        async def upload_bytes(self, data, key, *, content_type=None): self.store[key] = data
        async def download_bytes(self, key): return self.store[key]
        async def object_exists(self, key): return key in self.store

    from api.services.cc_chat.daytona_client import FakeDaytonaClient
    client = FakeDaytonaClient()
    sbx = await client.create_sandbox(env_vars={}, auto_stop_minutes=1, auto_delete_minutes=1, labels={})
    native_path = "/home/daytona/.claude/projects/-home-daytona-workspace/claude-xyz.jsonl"
    await client.upload_file(sbx, dest_path=native_path, content=b'{"line":1}\n')
    client.exec_sync_results = {"find": (0, native_path + "\n")}

    blob = _Blob()
    orch = ChatOrchestrator(
        daytona=client, runtime=_FakeRuntime(),
        transcript_buffer=SessionTranscriptBuffer(), anthropic_api_key="test", blob_store=blob,
    )
    orch._sandboxes["cs_1"] = sbx

    async for _ in orch.send(session_id="cs_1", content="hi", db_session_factory=factory):
        pass

    from api.services.cc_chat.archive import native_session_blob_key
    assert native_session_blob_key("cs_1") in blob.store

import pytest
from contextlib import asynccontextmanager
from tests.cc_chat.conftest import seed_session
from api.services.cc_chat.orchestrator import ChatOrchestrator
from api.services.cc_chat.transcript_buffer import SessionTranscriptBuffer
from api.services.cc_chat.daytona_client import FakeDaytonaClient
from api.services.cc_chat.archive import native_session_blob_key
from models import ChatSession, ChatStatus

pytestmark = pytest.mark.asyncio


class _Blob:
    def __init__(self): self.store = {}
    async def upload_bytes(self, data, key, *, content_type=None): self.store[key] = data
    async def download_bytes(self, key): return self.store[key]
    async def object_exists(self, key): return key in self.store


def _factory(db):
    def factory():
        @asynccontextmanager
        async def _cm():
            async with db() as s:
                yield s
        return _cm()
    return factory


class _Runtime:
    async def install(self, client, sandbox): ...


async def test_resume_restores_dead_sandbox(db):
    await seed_session(db, status="broken")
    async with db() as s:
        row = await s.get(ChatSession, "cs_1")
        row.claude_session_id = "claude-xyz"
        await s.commit()

    blob = _Blob()
    blob.store[native_session_blob_key("cs_1")] = b'{"line":1}\n'

    client = FakeDaytonaClient()
    orch = ChatOrchestrator(
        daytona=client, runtime=_Runtime(),
        transcript_buffer=SessionTranscriptBuffer(), anthropic_api_key="test",
        public_api_base_url="https://api.oddish.example", blob_store=blob,
    )

    await orch.resume(session_id="cs_1", db_session_factory=_factory(db))

    async with db() as s:
        row = await s.get(ChatSession, "cs_1")
    assert row.status == ChatStatus.active.value
    assert "cs_1" in orch._sandboxes
    new_id = orch._sandboxes["cs_1"].id
    dest = "/home/daytona/.claude/projects/-home-daytona-workspace/claude-xyz.jsonl"
    assert client.sandboxes[new_id]["files"][dest] == b'{"line":1}\n'


async def test_resume_no_archive_raises(db):
    await seed_session(db, status="broken")
    async with db() as s:
        row = await s.get(ChatSession, "cs_1")
        row.claude_session_id = "claude-xyz"
        await s.commit()

    client = FakeDaytonaClient()
    orch = ChatOrchestrator(
        daytona=client, runtime=_Runtime(),
        transcript_buffer=SessionTranscriptBuffer(), anthropic_api_key="test",
        public_api_base_url="https://api.oddish.example", blob_store=_Blob(),
    )
    from api.services.cc_chat.orchestrator import ResumeUnavailable
    with pytest.raises(ResumeUnavailable):
        await orch.resume(session_id="cs_1", db_session_factory=_factory(db))
    # the freshly-provisioned sandbox must be cleaned up on failure
    assert "cs_1" not in orch._sandboxes


async def test_resume_live_session_is_noop(db):
    await seed_session(db, status="active")
    client = FakeDaytonaClient()
    sbx = await client.create_sandbox(env_vars={}, auto_stop_minutes=1, auto_delete_minutes=1, labels={})
    orch = ChatOrchestrator(
        daytona=client, runtime=_Runtime(),
        transcript_buffer=SessionTranscriptBuffer(), anthropic_api_key="test", blob_store=_Blob(),
    )
    orch._sandboxes["cs_1"] = sbx
    await orch.resume(session_id="cs_1", db_session_factory=_factory(db))
    assert orch._sandboxes["cs_1"] is sbx  # unchanged

import pytest
from contextlib import asynccontextmanager
from api.services.cc_chat.daytona_client import FakeDaytonaClient
from api.services.cc_chat.orchestrator import ChatOrchestrator
from api.services.cc_chat.transcript_buffer import SessionTranscriptBuffer
from tests.cc_chat.conftest import ORG

pytestmark = pytest.mark.asyncio


class _FakeRuntime:
    async def install(self, client, sandbox):
        return None


class _FakeBlob:
    async def list_keys(self, prefix): return []
    async def download_bytes(self, key): return b""


async def test_start_sets_auto_delete_and_labels(db):
    def factory():
        @asynccontextmanager
        async def _cm():
            async with db() as s:
                yield s
        return _cm()

    fake = FakeDaytonaClient()
    orch = ChatOrchestrator(
        daytona=fake,
        runtime=_FakeRuntime(),
        transcript_buffer=SessionTranscriptBuffer(),
        anthropic_api_key="test",
        chat_auto_stop_minutes=30,
        chat_auto_delete_minutes=60,
        blob_store=_FakeBlob(),
    )

    session_id = await orch.start(
        org_id=ORG, user_id="u1",
        scope_kind="experiment", scope_id="exp_1",
        db_session_factory=factory,
    )

    rec = next(iter(fake.sandboxes.values()))
    assert rec["auto_stop"] == 30
    assert rec["auto_delete"] == 60
    assert rec["labels"] == {"app": "cc_chat", "session_id": session_id}

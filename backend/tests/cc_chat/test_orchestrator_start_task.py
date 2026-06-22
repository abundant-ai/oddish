import pytest
from contextlib import asynccontextmanager
from tests.cc_chat.conftest import ORG
from api.services.cc_chat.orchestrator import ChatOrchestrator
from api.services.cc_chat.transcript_buffer import SessionTranscriptBuffer

pytestmark = pytest.mark.asyncio


class _FakeSandbox:
    id = "sbx_task"


class _FakeDaytona:
    def __init__(self):
        self.uploaded: list[str] = []
    async def create_sandbox(self, *, env_vars, auto_stop_minutes, auto_delete_minutes, labels):
        return _FakeSandbox()
    async def create_session(self, sandbox, *, session_id):
        return None
    async def upload_file(self, sandbox, *, dest_path, content):
        self.uploaded.append(dest_path)
    async def exec_sync(self, sandbox, *, command):
        return 0, ""
    async def delete_sandbox(self, sandbox):
        return None


class _FakeRuntime:
    async def install(self, daytona, sandbox):
        return None


async def test_start_task_scope_uploads_cli_and_claude_md(db):
    def factory():
        @asynccontextmanager
        async def _cm():
            async with db() as s:
                yield s
        return _cm()

    daytona = _FakeDaytona()
    orch = ChatOrchestrator(
        daytona=daytona,
        runtime=_FakeRuntime(),
        transcript_buffer=SessionTranscriptBuffer(),
        anthropic_api_key="test",
        public_api_base_url="https://api.oddish.example",
    )
    session_id = await orch.start(
        org_id=ORG, user_id="u1", scope_kind="task", scope_id="demo-task",
        db_session_factory=factory,
    )
    assert session_id

    assert any(p.endswith("/CLAUDE.md") for p in daytona.uploaded)
    assert any(p.endswith("oddish-query") for p in daytona.uploaded)
    # No jobs/ file mounting
    assert not any("jobs/" in p for p in daytona.uploaded)

    from models import ChatSession
    async with db() as s:
        row = await s.get(ChatSession, session_id)
        assert row.status == "active" and row.scope_kind == "task"

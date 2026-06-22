"""TDD tests for Task 4: mint creds + upload CLI for all scopes."""
import pytest
from contextlib import asynccontextmanager

from api.services.cc_chat.daytona_client import CreatedSandbox
from api.services.cc_chat.transcript_buffer import SessionTranscriptBuffer
from tests.cc_chat.conftest import ORG

pytestmark = pytest.mark.asyncio


class _FakeSandbox:
    id = "sbx_scope_test"


class _FakeDaytona:
    def __init__(self):
        self.last_env_vars: dict = {}
        self.uploaded: list[str] = []
        self.sandboxes: dict = {}
        self.uploaded_query_cli: bool = False

    async def create_sandbox(self, *, env_vars, auto_stop_minutes, auto_delete_minutes, labels):
        sbx = _FakeSandbox()
        self.last_env_vars = env_vars
        self.sandboxes[sbx.id] = {"env": env_vars, "files": {}}
        return sbx

    async def create_session(self, sandbox, *, session_id):
        return None

    async def upload_file(self, sandbox, *, dest_path, content):
        self.uploaded.append(dest_path)
        if sandbox.id in self.sandboxes:
            self.sandboxes[sandbox.id]["files"][dest_path] = content
        if dest_path.endswith("oddish-query"):
            self.uploaded_query_cli = True

    async def exec_sync(self, sandbox, *, command):
        return 0, ""

    async def delete_sandbox(self, sandbox):
        return None


class _FakeRuntime:
    async def install(self, daytona, sandbox):
        return None


@pytest.mark.parametrize("scope", ["experiment", "task", "task_probes", "global"])
async def test_all_scopes_get_cli_and_creds(scope, db):
    from api.services.cc_chat import orchestrator as orch

    def factory():
        @asynccontextmanager
        async def _cm():
            async with db() as s:
                yield s
        return _cm()

    fake_daytona = _FakeDaytona()
    o = orch.ChatOrchestrator(
        daytona=fake_daytona,
        runtime=_FakeRuntime(),
        transcript_buffer=SessionTranscriptBuffer(),
        anthropic_api_key="a",
        public_api_base_url="https://api.example",
    )
    session_id = await o.start(
        org_id=ORG, user_id="u",
        scope_kind=scope,
        scope_id="sid",
        db_session_factory=factory,
    )
    assert session_id
    env = fake_daytona.last_env_vars
    assert env["ODDISH_API_KEY"].startswith("ok_")
    assert env["ODDISH_API_BASE_URL"] == "https://api.example"
    assert fake_daytona.uploaded_query_cli is True


async def test_missing_public_api_base_url_raises_for_all_scopes(db):
    from api.services.cc_chat import orchestrator as orch

    def factory():
        @asynccontextmanager
        async def _cm():
            async with db() as s:
                yield s
        return _cm()

    for scope in ["experiment", "task", "task_probes", "global"]:
        o = orch.ChatOrchestrator(
            daytona=_FakeDaytona(),
            runtime=_FakeRuntime(),
            transcript_buffer=SessionTranscriptBuffer(),
            anthropic_api_key="a",
            public_api_base_url="",
        )
        with pytest.raises(RuntimeError):
            await o.start(
                org_id=ORG, user_id="u",
                scope_kind=scope,
                scope_id="sid",
                db_session_factory=factory,
            )

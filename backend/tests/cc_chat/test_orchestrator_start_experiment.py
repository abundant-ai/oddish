import pytest
from contextlib import asynccontextmanager
from tests.cc_chat.conftest import seed_task_with_trials, ORG
from api.services.cc_chat.orchestrator import ChatOrchestrator
from api.services.cc_chat.transcript_buffer import SessionTranscriptBuffer

pytestmark = pytest.mark.asyncio


class _FakeSandbox:
    id = "sbx_exp"


class _FakeDaytona:
    def __init__(self):
        self.uploaded: list[str] = []
    async def create_sandbox(self, *, env_vars, auto_stop_minutes, auto_delete_minutes, labels):
        return _FakeSandbox()
    async def create_session(self, sandbox, *, session_id):
        return None
    async def upload_file(self, sandbox, *, dest_path, content):
        self.uploaded.append(dest_path)
    async def delete_sandbox(self, sandbox):
        return None


class _FakeRuntime:
    async def install(self, daytona, sandbox):
        return None


class _FakeStorage:
    async def list_keys(self, prefix):
        return [f"{prefix}result.json", f"{prefix}agent/trajectory.json"]
    async def download_bytes(self, key):
        return b"bytes"


async def test_start_experiment_scope_uploads_jobs_tree_and_claude_md(db):
    # seed_task_with_trials creates experiment "exp_task_1" with the trials.
    await seed_task_with_trials(db, versions=(1, 2), trials_per_version=1)

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
        blob_store=_FakeStorage(),
    )
    session_id = await orch.start(
        org_id=ORG, user_id="u1", scope_kind="experiment", scope_id="exp_task_1",
        db_session_factory=factory,
    )
    assert session_id

    # The bug: only CLAUDE.md used to be uploaded. Now the jobs tree is mounted
    # under jobs/{experiment_id}/{trial_id}/ as the CLAUDE.md template promises.
    assert any(p.endswith("/CLAUDE.md") for p in daytona.uploaded)
    assert any("jobs/exp_task_1/task_1-10/result.json" in p for p in daytona.uploaded)
    assert any("jobs/exp_task_1/" in p and p.endswith("agent/trajectory.json")
               for p in daytona.uploaded)

    from models import ChatSession
    async with db() as s:
        row = await s.get(ChatSession, session_id)
        assert row.status == "active" and row.scope_kind == "experiment"

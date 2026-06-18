from contextlib import asynccontextmanager

import pytest

from tests.cc_chat.conftest import seed_task_with_trials, ORG

pytestmark = pytest.mark.asyncio


class _FakeRuntime:
    async def install(self, client, sandbox):
        return None


def _factory(db):
    def factory():
        @asynccontextmanager
        async def _cm():
            async with db() as s:
                yield s
        return _cm()
    return factory


async def test_experiment_scope_mints_key_uploads_cli_and_mounts_no_files(db, monkeypatch):
    from api.services.cc_chat import orchestrator as orchestrator_module
    from api.services.cc_chat.daytona_client import FakeDaytonaClient
    from api.services.cc_chat.orchestrator import ChatOrchestrator
    from api.services.cc_chat.transcript_buffer import SessionTranscriptBuffer
    from models import APIKeyModel, APIKeyScope, ChatSession, generate_id

    await seed_task_with_trials(db, versions=(1, 2), trials_per_version=1)

    minted: dict[str, object] = {}

    def fake_create_api_key(
        org_id, name, scope=APIKeyScope.FULL, created_by_user_id=None,
        expires_at=None, is_internal=False,
    ):
        model = APIKeyModel(
            id=generate_id(), org_id=org_id, name=name,
            key_prefix="ok_testkey", key_hash=f"hash_{generate_id()}",
            scope=scope, created_by_user_id=None,
            expires_at=expires_at, is_internal=is_internal,
        )
        minted["model"] = model
        minted["scope"] = scope
        return model, "ok_rawsecretkey"

    monkeypatch.setattr(orchestrator_module, "create_api_key", fake_create_api_key)

    fake = FakeDaytonaClient()
    orch = ChatOrchestrator(
        daytona=fake,
        runtime=_FakeRuntime(),
        transcript_buffer=SessionTranscriptBuffer(),
        anthropic_api_key="test",
        public_api_base_url="https://api.oddish.example",
    )

    session_id = await orch.start(
        org_id=ORG, user_id=None,
        scope_kind="experiment", scope_id="exp_task_1",
        db_session_factory=_factory(db),
    )
    assert session_id

    rec = next(iter(fake.sandboxes.values()))
    uploaded = list(rec["files"].keys())
    # CLI + CLAUDE.md uploaded; NO jobs/ artifacts mounted
    assert any(p.endswith("oddish-query") for p in uploaded)
    assert any(p.endswith("/CLAUDE.md") for p in uploaded)
    assert not any("jobs/" in p for p in uploaded)
    # read-only query credential injected
    assert minted["scope"] == APIKeyScope.READ
    assert rec["env"]["ODDISH_API_KEY"] == "ok_rawsecretkey"
    assert rec["env"]["ODDISH_API_BASE_URL"] == "https://api.oddish.example"

    async with db() as s:
        row = await s.get(ChatSession, session_id)
        assert row.status == "active" and row.scope_kind == "experiment"
        assert row.query_api_key_id == minted["model"].id


async def test_experiment_scope_resume_uploads_cli_and_mints_fresh_key(db, monkeypatch):
    from api.services.cc_chat import orchestrator as orchestrator_module
    from api.services.cc_chat.archive import native_session_blob_key
    from api.services.cc_chat.daytona_client import FakeDaytonaClient
    from api.services.cc_chat.orchestrator import ChatOrchestrator
    from api.services.cc_chat.transcript_buffer import SessionTranscriptBuffer
    from models import APIKeyModel, APIKeyScope, ChatSession, generate_id

    await seed_task_with_trials(db, versions=(1, 2), trials_per_version=1)

    minted: dict[str, object] = {}

    def fake_create_api_key(
        org_id, name, scope=APIKeyScope.FULL, created_by_user_id=None,
        expires_at=None, is_internal=False,
    ):
        model = APIKeyModel(
            id=generate_id(), org_id=org_id, name=name,
            key_prefix="ok_testkey", key_hash=f"hash_{generate_id()}",
            scope=scope, created_by_user_id=None,
            expires_at=expires_at, is_internal=is_internal,
        )
        minted["model"] = model
        minted["scope"] = scope
        return model, "ok_rawsecretkey"

    monkeypatch.setattr(orchestrator_module, "create_api_key", fake_create_api_key)

    class _Blob:
        def __init__(self): self.store = {}
        async def upload_bytes(self, data, key, *, content_type=None): self.store[key] = data
        async def download_bytes(self, key): return self.store[key]
        async def object_exists(self, key): return key in self.store

    blob = _Blob()
    fake = FakeDaytonaClient()
    orch = ChatOrchestrator(
        daytona=fake,
        runtime=_FakeRuntime(),
        transcript_buffer=SessionTranscriptBuffer(),
        anthropic_api_key="test",
        public_api_base_url="https://api.oddish.example",
        blob_store=blob,
    )

    session_id = await orch.start(
        org_id=ORG, user_id=None,
        scope_kind="experiment", scope_id="exp_task_1",
        db_session_factory=_factory(db),
    )
    key_a_id = minted["model"].id

    # Detach in-process, set a claude_session_id, and stage an archive so resume
    # provisions a fresh sandbox and restores successfully.
    orch._sandboxes.pop(session_id, None)
    async with db() as s:
        row = await s.get(ChatSession, session_id)
        row.claude_session_id = "claude-xyz"
        await s.commit()
    blob.store[native_session_blob_key(session_id)] = b'{"line":1}\n'

    await orch.resume(session_id=session_id, db_session_factory=_factory(db))

    # resume creates a NEW sandbox; assert against the one present after resume.
    rec = fake.sandboxes[next(reversed(fake.sandboxes))]
    # (a) the oddish-query CLI was uploaded into the rebuilt sandbox
    assert any(p.endswith("oddish-query") for p in rec["files"].keys())
    # (b) a fresh READ key was minted and injected
    key_b_id = minted["model"].id
    assert key_b_id != key_a_id
    assert minted["scope"] == APIKeyScope.READ
    assert rec["env"]["ODDISH_API_KEY"] == "ok_rawsecretkey"
    assert rec["env"]["ODDISH_API_BASE_URL"] == "https://api.oddish.example"
    async with db() as s:
        row = await s.get(ChatSession, session_id)
        assert row.query_api_key_id == key_b_id

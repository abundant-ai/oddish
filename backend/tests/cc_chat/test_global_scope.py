from contextlib import asynccontextmanager

import pytest

from api.services.cc_chat.claude_md import render_global_claude_md


def test_global_claude_md_documents_cli_and_discipline():
    md = render_global_claude_md(org_id="org_42")
    assert "oddish-query tasks search" in md
    assert "oddish-query trials logs" in md
    # shallow-by-default steering
    assert "search" in md.lower()
    assert "one trial at a time" in md.lower()


class _FakeRuntime:
    async def install(self, client, sandbox):
        return None


@pytest.mark.asyncio
async def test_global_scope_mints_read_key_injects_env_and_uploads_cli(db, monkeypatch):
    from api.services.cc_chat import orchestrator as orchestrator_module
    from api.services.cc_chat.daytona_client import FakeDaytonaClient
    from api.services.cc_chat.orchestrator import ChatOrchestrator
    from api.services.cc_chat.transcript_buffer import SessionTranscriptBuffer
    from models import ChatSession
    from tests.cc_chat.conftest import ORG

    def factory():
        @asynccontextmanager
        async def _cm():
            async with db() as s:
                yield s
        return _cm()

    minted: dict[str, object] = {}

    async def fake_mint_internal_read_key(session, *, org_id, name, ttl_minutes):
        from models import APIKeyModel, APIKeyScope, generate_id
        model = APIKeyModel(
            id=generate_id(),
            org_id=org_id,
            name=name,
            key_prefix="ok_testkey",
            key_hash=f"hash_{generate_id()}",
            scope=APIKeyScope.READ,
            created_by_user_id=None,
            expires_at=None,
            is_internal=True,
        )
        session.add(model)
        await session.commit()
        minted["model"] = model
        return model.id, "ok_rawsecretkey"

    monkeypatch.setattr(orchestrator_module, "mint_internal_read_key", fake_mint_internal_read_key)

    fake = FakeDaytonaClient()
    orch = ChatOrchestrator(
        daytona=fake,
        runtime=_FakeRuntime(),
        transcript_buffer=SessionTranscriptBuffer(),
        anthropic_api_key="test",
        chat_auto_stop_minutes=30,
        chat_auto_delete_minutes=60,
        public_api_base_url="https://api.oddish.example",
    )

    session_id = await orch.start(
        org_id=ORG, user_id=None,
        scope_kind="global", scope_id=ORG,
        db_session_factory=factory,
    )

    # (a) a key was minted
    assert "model" in minted

    # (b) the provisioner env carries the query credentials
    rec = next(iter(fake.sandboxes.values()))
    env = rec["env"]
    assert env["ODDISH_API_KEY"] == "ok_rawsecretkey"
    assert env["ODDISH_API_BASE_URL"] == "https://api.oddish.example"

    # (c) the CLI was uploaded into the workspace
    uploaded = list(rec["files"].keys())
    assert any(path.endswith("oddish-query") for path in uploaded)

    # (d) the session row records the minted key id
    async with db() as s:
        row = await s.get(ChatSession, session_id)
        assert row.query_api_key_id == minted["model"].id


def _factory(db):
    def factory():
        @asynccontextmanager
        async def _cm():
            async with db() as s:
                yield s
        return _cm()
    return factory


def _patch_mint_key(monkeypatch, minted):
    """Patch mint_internal_read_key to record the minted model and return a raw key."""
    from api.services.cc_chat import orchestrator as orchestrator_module
    from models import APIKeyModel, APIKeyScope, generate_id

    async def fake_mint_internal_read_key(session, *, org_id, name, ttl_minutes):
        model = APIKeyModel(
            id=generate_id(),
            org_id=org_id,
            name=name,
            key_prefix="ok_testkey",
            key_hash=f"hash_{generate_id()}",
            scope=APIKeyScope.READ,
            created_by_user_id=None,
            expires_at=None,
            is_internal=True,
        )
        session.add(model)
        await session.commit()
        minted["model"] = model
        return model.id, "ok_rawsecretkey"

    monkeypatch.setattr(orchestrator_module, "mint_internal_read_key", fake_mint_internal_read_key)


@pytest.mark.asyncio
async def test_global_scope_close_revokes_key(db, monkeypatch):
    from api.services.cc_chat.daytona_client import FakeDaytonaClient
    from api.services.cc_chat.orchestrator import ChatOrchestrator
    from api.services.cc_chat.transcript_buffer import SessionTranscriptBuffer
    from models import APIKeyModel, ChatSession
    from tests.cc_chat.conftest import ORG

    minted: dict[str, object] = {}
    _patch_mint_key(monkeypatch, minted)

    orch = ChatOrchestrator(
        daytona=FakeDaytonaClient(),
        runtime=_FakeRuntime(),
        transcript_buffer=SessionTranscriptBuffer(),
        anthropic_api_key="test",
        public_api_base_url="https://api.oddish.example",
    )

    session_id = await orch.start(
        org_id=ORG, user_id=None,
        scope_kind="global", scope_id=ORG,
        db_session_factory=_factory(db),
    )

    async with db() as s:
        row = await s.get(ChatSession, session_id)
        key_id = row.query_api_key_id
    assert key_id is not None
    async with db() as s:
        assert await s.get(APIKeyModel, key_id) is not None

    await orch.close(session_id=session_id, db_session_factory=_factory(db))

    async with db() as s:
        assert await s.get(APIKeyModel, key_id) is None


@pytest.mark.asyncio
async def test_global_scope_resume_revokes_prior_key(db, monkeypatch):
    from api.services.cc_chat.archive import native_session_blob_key
    from api.services.cc_chat.daytona_client import FakeDaytonaClient
    from api.services.cc_chat.orchestrator import ChatOrchestrator
    from api.services.cc_chat.transcript_buffer import SessionTranscriptBuffer
    from models import APIKeyModel, ChatSession
    from tests.cc_chat.conftest import ORG

    minted: dict[str, object] = {}
    _patch_mint_key(monkeypatch, minted)

    class _Blob:
        def __init__(self): self.store = {}
        async def upload_bytes(self, data, key, *, content_type=None): self.store[key] = data
        async def download_bytes(self, key): return self.store[key]
        async def object_exists(self, key): return key in self.store

    blob = _Blob()
    orch = ChatOrchestrator(
        daytona=FakeDaytonaClient(),
        runtime=_FakeRuntime(),
        transcript_buffer=SessionTranscriptBuffer(),
        anthropic_api_key="test",
        public_api_base_url="https://api.oddish.example",
        blob_store=blob,
    )

    session_id = await orch.start(
        org_id=ORG, user_id=None,
        scope_kind="global", scope_id=ORG,
        db_session_factory=_factory(db),
    )
    key_a_id = minted["model"].id

    orch._sandboxes.pop(session_id, None)
    async with db() as s:
        row = await s.get(ChatSession, session_id)
        row.claude_session_id = "claude-xyz"
        await s.commit()
    blob.store[native_session_blob_key(session_id)] = b'{"line":1}\n'

    await orch.resume(session_id=session_id, db_session_factory=_factory(db))

    key_b_id = minted["model"].id
    assert key_b_id != key_a_id
    async with db() as s:
        row = await s.get(ChatSession, session_id)
        assert row.query_api_key_id == key_b_id
        assert await s.get(APIKeyModel, key_a_id) is None


@pytest.mark.asyncio
async def test_empty_base_url_fast_fails(db):
    """All scopes should raise RuntimeError when public_api_base_url is empty."""
    from api.services.cc_chat.daytona_client import FakeDaytonaClient
    from api.services.cc_chat.orchestrator import ChatOrchestrator
    from api.services.cc_chat.transcript_buffer import SessionTranscriptBuffer
    from tests.cc_chat.conftest import ORG

    for scope in ["global", "experiment", "task", "task_probes"]:
        orch = ChatOrchestrator(
            daytona=FakeDaytonaClient(),
            runtime=_FakeRuntime(),
            transcript_buffer=SessionTranscriptBuffer(),
            anthropic_api_key="test",
            public_api_base_url="",
        )

        with pytest.raises(RuntimeError):
            await orch.start(
                org_id=ORG, user_id=None,
                scope_kind=scope, scope_id=ORG,
                db_session_factory=_factory(db),
            )


@pytest.mark.asyncio
async def test_all_scopes_mint_key_and_upload_cli(db, monkeypatch):
    """Every scope now gets creds injected + the CLI uploaded."""
    from api.services.cc_chat.daytona_client import FakeDaytonaClient
    from api.services.cc_chat.orchestrator import ChatOrchestrator
    from api.services.cc_chat.transcript_buffer import SessionTranscriptBuffer
    from tests.cc_chat.conftest import ORG

    minted: dict[str, object] = {}
    _patch_mint_key(monkeypatch, minted)

    for scope in ["experiment", "task", "task_probes", "global"]:
        fake = FakeDaytonaClient()
        orch = ChatOrchestrator(
            daytona=fake,
            runtime=_FakeRuntime(),
            transcript_buffer=SessionTranscriptBuffer(),
            anthropic_api_key="test",
            public_api_base_url="https://api.oddish.example",
        )
        await orch.start(
            org_id=ORG, user_id=None,
            scope_kind=scope, scope_id="sid",
            db_session_factory=_factory(db),
        )
        rec = next(iter(fake.sandboxes.values()))
        assert rec["env"]["ODDISH_API_KEY"] == "ok_rawsecretkey", f"missing creds for {scope}"
        assert any(p.endswith("oddish-query") for p in rec["files"]), f"missing CLI for {scope}"

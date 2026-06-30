"""Core CRUD + ingest tests for the agent doc-store.

Exercises ``oddish.core.documents`` against the real local Postgres (same
pattern as ``test_probe_presets.py``). The Claude digest call and S3 upload
are patched so the test is deterministic and offline. Run with the backend
env sourced and the ``documents_001`` migration applied:

    set -a && source .env && set +a && uv run pytest tests/test_documents.py
"""

import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException

from oddish.core.sharing import documents as docs
from oddish.core.digest import DigestResult
from oddish.db import DocumentModel, get_session
from oddish.schemas import DocumentCreate, DocumentUpdate


@pytest_asyncio.fixture
async def org_id():
    oid = f"org_doc_{uuid.uuid4().hex[:8]}"
    yield oid
    async with get_session() as session:
        await session.execute(
            DocumentModel.__table__.delete().where(DocumentModel.org_id == oid)
        )
        await session.commit()


@pytest.fixture(autouse=True)
def _patch_io(monkeypatch):
    async def fake_digest(*, title, text, model="claude-sonnet-4-6"):
        return DigestResult(
            summary=f"sum:{title}", digest_text=f"dig:{text[:10]}", tags=["t1"]
        )

    monkeypatch.setattr(docs, "generate_digest", fake_digest)

    class _FakeClient:
        async def upload_bytes(self, data, s3_key, *, content_type=None):
            return None

    monkeypatch.setattr(docs, "get_storage_client", lambda: _FakeClient())


@pytest.mark.asyncio
async def test_create_runs_digest_and_persists(org_id):
    data = DocumentCreate(
        title="QA Doc", source_type="paste", content="reset by clicking reset"
    )
    async with get_session() as session:
        doc = await docs.create_document_core(
            session, data=data, org_id=org_id, user_id="u1"
        )
        await session.commit()
        assert doc.summary == "sum:QA Doc"
        assert doc.digest_text.startswith("dig:")
        assert doc.tags == ["t1"]
        assert doc.created_by_user_id == "u1"
        assert doc.content_text == "reset by clicking reset"
        assert doc.s3_key_raw and doc.s3_key_raw.endswith("/raw")


@pytest.mark.asyncio
async def test_create_requires_content_or_file(org_id):
    data = DocumentCreate(title="empty", source_type="paste")
    async with get_session() as session:
        with pytest.raises(HTTPException) as ei:
            await docs.create_document_core(
                session, data=data, org_id=org_id, user_id="u1"
            )
        assert ei.value.status_code == 422


@pytest.mark.asyncio
async def test_list_orders_by_updated_at_desc(org_id):
    async with get_session() as session:
        for t in ("first", "second"):
            d = DocumentCreate(title=t, source_type="paste", content=t)
            await docs.create_document_core(
                session, data=d, org_id=org_id, user_id="u1"
            )
        await session.commit()
        rows = await docs.list_documents_core(
            session, org_id=org_id, limit=10, offset=0
        )
        assert [r.title for r in rows] == ["second", "first"]


@pytest.mark.asyncio
async def test_update_metadata(org_id):
    async with get_session() as session:
        d = await docs.create_document_core(
            session,
            data=DocumentCreate(title="x", source_type="paste", content="c"),
            org_id=org_id,
            user_id="u1",
        )
        await session.commit()
        upd = await docs.update_document_core(
            session,
            d.id,
            data=DocumentUpdate(title="renamed", tags=["new"]),
            org_id=org_id,
        )
        await session.commit()
        assert upd.title == "renamed" and upd.tags == ["new"]


@pytest.mark.asyncio
async def test_get_other_org_404(org_id):
    async with get_session() as session:
        d = await docs.create_document_core(
            session,
            data=DocumentCreate(title="x", source_type="paste", content="c"),
            org_id=org_id,
            user_id="u1",
        )
        await session.commit()
        with pytest.raises(HTTPException) as ei:
            await docs.get_document_core(session, d.id, org_id="someone_else")
        assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_soft_deletes(org_id):
    async with get_session() as session:
        d = await docs.create_document_core(
            session,
            data=DocumentCreate(title="x", source_type="paste", content="c"),
            org_id=org_id,
            user_id="u1",
        )
        await session.commit()
        await docs.delete_document_core(session, d.id, org_id=org_id)
        await session.commit()
        rows = await docs.list_documents_core(
            session, org_id=org_id, limit=10, offset=0
        )
        assert rows == []

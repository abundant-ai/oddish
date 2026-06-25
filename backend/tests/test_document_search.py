"""Tests for keyword search over the doc-store (``search_documents_core``)."""

import uuid

import pytest
import pytest_asyncio

from oddish.core.sharing import documents as docs
from oddish.db import DocumentModel, get_session, utcnow


@pytest_asyncio.fixture
async def org_id():
    oid = f"org_ds_{uuid.uuid4().hex[:8]}"
    yield oid
    async with get_session() as session:
        await session.execute(
            DocumentModel.__table__.delete().where(DocumentModel.org_id == oid)
        )
        await session.commit()


async def _mk(session, org_id, *, title, digest, tags=None):
    doc = DocumentModel(
        org_id=org_id,
        created_by_user_id="u1",
        title=title,
        source_type="paste",
        summary="s",
        digest_text=digest,
        tags=tags or [],
        content_text=digest,
    )
    session.add(doc)
    await session.flush()
    return doc


@pytest.mark.asyncio
async def test_title_matches_rank_before_content_matches(org_id):
    async with get_session() as session:
        await _mk(
            session, org_id, title="unrelated", digest="contains kubernetes notes"
        )
        await _mk(session, org_id, title="kubernetes guide", digest="nothing here")
        await session.flush()
        rows = await docs.search_documents_core(
            session, org_id=org_id, query="kubernetes", tags=None, limit=10
        )
        assert rows[0].title == "kubernetes guide"  # title hit ranks first
        assert len(rows) == 2


@pytest.mark.asyncio
async def test_no_query_returns_all_by_updated_desc(org_id):
    async with get_session() as session:
        await _mk(session, org_id, title="a", digest="x")
        b = await _mk(session, org_id, title="b", digest="y")
        b.updated_at = utcnow()
        await session.flush()
        rows = await docs.search_documents_core(
            session, org_id=org_id, query="", tags=None, limit=10
        )
        assert {r.title for r in rows} == {"a", "b"}


@pytest.mark.asyncio
async def test_tag_filter(org_id):
    async with get_session() as session:
        await _mk(session, org_id, title="t1", digest="d", tags=["aws"])
        await _mk(session, org_id, title="t2", digest="d", tags=["gcp"])
        await session.flush()
        rows = await docs.search_documents_core(
            session, org_id=org_id, query="", tags=["aws"], limit=10
        )
        assert [r.title for r in rows] == ["t1"]


@pytest.mark.asyncio
async def test_query_no_match_returns_empty(org_id):
    async with get_session() as session:
        await _mk(session, org_id, title="t1", digest="d")
        await session.flush()
        rows = await docs.search_documents_core(
            session, org_id=org_id, query="zzz-nomatch", tags=None, limit=10
        )
        assert rows == []

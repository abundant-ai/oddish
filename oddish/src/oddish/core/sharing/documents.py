"""Core CRUD + ingest for the agent doc-store.

Receives an ``AsyncSession`` and never commits — the caller owns the
transaction. Ingest orchestrates: extract text -> Claude digest -> store raw
bytes in S3 -> persist the row. The soft-delete listener auto-applies
``deleted_at IS NULL`` to reads.
"""

from __future__ import annotations

import base64

from fastapi import HTTPException
from sqlalchemy import case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from oddish.core.digest import generate_digest
from oddish.core.ingest.extraction import extract_text
from oddish.core.helpers import escape_like
from oddish.db import DocumentModel, get_storage_client, utcnow
from oddish.schemas import DocumentCreate, DocumentUpdate


def _raw_bytes(data: DocumentCreate) -> bytes | None:
    if data.file_b64 is not None:
        return base64.b64decode(data.file_b64)
    if data.content is not None:
        return data.content.encode("utf-8")
    return None


def _derive_title(data: DocumentCreate, text: str) -> str:
    if data.title:
        return data.title
    if data.raw_filename:
        return data.raw_filename
    first = text.strip().splitlines()[0] if text.strip() else "Untitled"
    return first[:120] or "Untitled"


async def create_document_core(
    session: AsyncSession,
    *,
    data: DocumentCreate,
    org_id: str | None,
    user_id: str | None,
) -> DocumentModel:
    """Ingest a document: extract -> digest -> store raw -> persist."""
    raw = _raw_bytes(data)
    if raw is None:
        raise HTTPException(status_code=422, detail="content or file_b64 required")

    text = extract_text(raw, mime=data.raw_mime, filename=data.raw_filename)
    if not text.strip():
        raise HTTPException(status_code=422, detail="no extractable text in document")

    title = _derive_title(data, text)
    digest = await generate_digest(title=title, text=text)

    doc = DocumentModel(
        org_id=org_id,
        created_by_user_id=user_id,
        title=title,
        source_type=data.source_type,
        source_url=data.source_url,
        summary=digest.summary,
        digest_text=digest.digest_text,
        tags=digest.tags,
        content_text=text,
        raw_mime=data.raw_mime,
        raw_filename=data.raw_filename,
    )
    session.add(doc)
    await session.flush()  # assign doc.id

    # Store raw bytes in S3 under a stable, org-scoped key.
    key = f"documents/{org_id or 'global'}/{doc.id}/raw"
    await get_storage_client().upload_bytes(
        raw, key, content_type=data.raw_mime or "text/plain"
    )
    doc.s3_key_raw = key
    await session.flush()
    return doc


async def list_documents_core(
    session: AsyncSession,
    *,
    org_id: str | None,
    limit: int = 50,
    offset: int = 0,
) -> list[DocumentModel]:
    """List the org's documents, newest-updated first (paginated)."""
    query = (
        select(DocumentModel)
        .where(DocumentModel.org_id == org_id)
        .order_by(DocumentModel.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(query)
    return list(result.scalars().all())


async def search_documents_core(
    session: AsyncSession,
    *,
    org_id: str | None,
    query: str,
    tags: list[str] | None = None,
    limit: int = 20,
) -> list[DocumentModel]:
    """Keyword search over title + digest, title-matches ranked first.

    Empty ``query`` lists all org docs by recency. ``tags`` (if given) is an
    array-overlap filter. Ordering: title hits (0) before content-only hits
    (1), then newest-updated first.
    """
    stmt = select(DocumentModel).where(DocumentModel.org_id == org_id)
    q = (query or "").strip()
    if q:
        like = f"%{escape_like(q)}%"
        title_hit = DocumentModel.title.ilike(like, escape="\\")
        stmt = stmt.where(
            or_(title_hit, DocumentModel.digest_text.ilike(like, escape="\\"))
        )
        rank: ColumnElement = case((title_hit, 0), else_=1)
        stmt = stmt.order_by(rank, DocumentModel.updated_at.desc())
    else:
        stmt = stmt.order_by(DocumentModel.updated_at.desc())
    if tags:
        stmt = stmt.where(DocumentModel.tags.overlap(tags))
    stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_document_core(
    session: AsyncSession, document_id: str, *, org_id: str | None
) -> DocumentModel:
    """Fetch one document scoped to the org, or 404."""
    query = select(DocumentModel).where(
        DocumentModel.id == document_id, DocumentModel.org_id == org_id
    )
    doc = (await session.execute(query)).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    return doc


async def update_document_core(
    session: AsyncSession,
    document_id: str,
    *,
    data: DocumentUpdate,
    org_id: str | None,
) -> DocumentModel:
    """Patch metadata; optionally re-run the digest."""
    doc = await get_document_core(session, document_id, org_id=org_id)
    if data.title is not None:
        doc.title = data.title
    if data.summary is not None:
        doc.summary = data.summary
    if data.tags is not None:
        doc.tags = data.tags
    if data.regenerate_digest:
        digest = await generate_digest(title=doc.title, text=doc.content_text)
        doc.summary = digest.summary
        doc.digest_text = digest.digest_text
        doc.tags = digest.tags
    await session.flush()
    return doc


async def delete_document_core(
    session: AsyncSession, document_id: str, *, org_id: str | None
) -> None:
    """Soft-delete: stamp ``deleted_at``."""
    doc = await get_document_core(session, document_id, org_id=org_id)
    doc.deleted_at = utcnow()
    await session.flush()

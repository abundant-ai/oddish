"""MCP server exposing the agent doc-store for networked Claude Code sessions.

Three tools, tiered to keep agent context cheap:
  search_documents -> cheap cards (title+summary+tags)
  get_document     -> the tier-2 digest
  inspect_source   -> the full extracted source text (escape hatch)

Org scope is fixed per-process via ``ODDISH_DOCSTORE_ORG_ID`` (unset = global).
Tools delegate to ``oddish.core.documents``; reads only.
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from oddish.core.sharing.documents import get_document_core, search_documents_core
from oddish.db import DocumentModel, get_session

mcp = FastMCP("oddish-docstore")


def _scoped_org_id() -> str | None:
    return os.environ.get("ODDISH_DOCSTORE_ORG_ID") or None


def _card_dict(doc: DocumentModel) -> dict[str, Any]:
    return {
        "id": doc.id,
        "title": doc.title,
        "summary": doc.summary,
        "tags": list(doc.tags or []),
    }


def _doc_detail(doc: DocumentModel) -> dict[str, Any]:
    return {
        "id": doc.id,
        "title": doc.title,
        "digest": doc.digest_text,
        "tags": list(doc.tags or []),
        "source_url": doc.source_url,
    }


@mcp.tool()
async def search_documents(
    query: str = "", tags: list[str] | None = None
) -> list[dict]:
    """Search the team doc-store. Returns lightweight cards (id, title, summary,
    tags) ranked with title matches first. Empty query lists recent docs. Use
    `get_document(id)` to read a result's full digest."""
    async with get_session() as session:
        rows = await search_documents_core(
            session, org_id=_scoped_org_id(), query=query, tags=tags, limit=20
        )
        return [_card_dict(r) for r in rows]


@mcp.tool()
async def get_document(id: str) -> dict:
    """Fetch a document's agent-optimized digest (the cleaned, condensed version
    meant for acting on). If the digest is missing a detail, call
    `inspect_source(id)` for the full original text."""
    async with get_session() as session:
        doc = await get_document_core(session, id, org_id=_scoped_org_id())
        return _doc_detail(doc)


@mcp.tool()
async def inspect_source(id: str) -> dict:
    """Return the full extracted source text of a document — the highest-fidelity
    escape hatch when the digest lost a detail. Larger than the digest; only use
    when `get_document` wasn't enough."""
    async with get_session() as session:
        doc = await get_document_core(session, id, org_id=_scoped_org_id())
        return {
            "id": doc.id,
            "title": doc.title,
            "source_type": doc.source_type,
            "source_url": doc.source_url,
            "raw_filename": doc.raw_filename,
            "content": doc.content_text,
        }


def main() -> None:
    """Run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()

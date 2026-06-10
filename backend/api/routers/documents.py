"""CRUD endpoints for the agent doc-store.

Thin wrapper over ``oddish.core.documents``: authenticate, open a session,
delegate to the core function, commit, serialize. Org-scoped via the auth
context.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from auth import APIKeyScope, AuthContext, require_auth
from oddish.core.documents import (
    create_document_core,
    delete_document_core,
    get_document_core,
    list_documents_core,
    update_document_core,
)
from oddish.db import get_session
from oddish.schemas import (
    DocumentCard,
    DocumentCreate,
    DocumentResponse,
    DocumentUpdate,
)

router = APIRouter()


@router.get("/documents", response_model=list[DocumentCard])
async def list_documents(
    auth: Annotated[AuthContext, Depends(require_auth)],
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
) -> list[DocumentCard]:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        rows = await list_documents_core(
            session, org_id=auth.org_id, limit=limit, offset=offset
        )
        return [DocumentCard.model_validate(r) for r in rows]


@router.get("/documents/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> DocumentResponse:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        doc = await get_document_core(session, document_id, org_id=auth.org_id)
        return DocumentResponse.model_validate(doc)


@router.post("/documents", response_model=DocumentResponse)
async def create_document(
    data: DocumentCreate,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> DocumentResponse:
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        doc = await create_document_core(
            session, data=data, org_id=auth.org_id, user_id=auth.user_id
        )
        await session.commit()
        return DocumentResponse.model_validate(doc)


@router.put("/documents/{document_id}", response_model=DocumentResponse)
async def update_document(
    document_id: str,
    data: DocumentUpdate,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> DocumentResponse:
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        doc = await update_document_core(
            session, document_id, data=data, org_id=auth.org_id
        )
        await session.commit()
        return DocumentResponse.model_validate(doc)


@router.delete("/documents/{document_id}")
async def delete_document(
    document_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict[str, bool]:
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        await delete_document_core(session, document_id, org_id=auth.org_id)
        await session.commit()
        return {"ok": True}

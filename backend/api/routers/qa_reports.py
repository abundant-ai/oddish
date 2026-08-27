"""Hosted, org-scoped controls for curated experiment QA reports."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from auth import APIKeyScope, AuthContext, require_admin, require_auth
from oddish.core.qa_reports import (
    create_qa_report_core,
    get_qa_report_core,
    patch_qa_report_core,
    preview_qa_report_core,
    publish_qa_report_core,
    sync_qa_report_core,
    unpublish_qa_report_core,
)
from oddish.db import get_read_session, get_session
from oddish.schemas import (
    PublicQAReportResponse,
    QAReportCreateRequest,
    QAReportPatchRequest,
    QAReportPublishRequest,
    QAReportResponse,
    QAReportUnpublishRequest,
)


router = APIRouter(tags=["QA Reports"])


@router.get("/experiments/{experiment_id}/qa", response_model=QAReportResponse)
async def get_experiment_qa_report(
    experiment_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> QAReportResponse:
    auth.require_scope(APIKeyScope.READ)
    assert auth.org_id is not None
    async with get_read_session() as session:
        return await get_qa_report_core(
            session, experiment_id=experiment_id, org_id=auth.org_id
        )


@router.post("/experiments/{experiment_id}/qa", response_model=QAReportResponse)
async def create_experiment_qa_report(
    experiment_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
    payload: QAReportCreateRequest | None = None,
) -> QAReportResponse:
    assert auth.org_id is not None
    async with get_session() as session:
        response = await create_qa_report_core(
            session,
            experiment_id=experiment_id,
            org_id=auth.org_id,
            created_by_user_id=auth.user_id,
            payload=payload,
        )
        await session.commit()
        return response


@router.patch("/experiments/{experiment_id}/qa", response_model=QAReportResponse)
async def patch_experiment_qa_report(
    experiment_id: str,
    payload: QAReportPatchRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> QAReportResponse:
    assert auth.org_id is not None
    async with get_session() as session:
        response = await patch_qa_report_core(
            session,
            experiment_id=experiment_id,
            org_id=auth.org_id,
            payload=payload,
        )
        await session.commit()
        return response


@router.post("/experiments/{experiment_id}/qa/sync", response_model=QAReportResponse)
async def sync_experiment_qa_report(
    experiment_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> QAReportResponse:
    assert auth.org_id is not None
    async with get_session() as session:
        response = await sync_qa_report_core(
            session, experiment_id=experiment_id, org_id=auth.org_id
        )
        await session.commit()
        return response


@router.get(
    "/experiments/{experiment_id}/qa/preview",
    response_model=PublicQAReportResponse,
)
async def preview_experiment_qa_report(
    experiment_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> PublicQAReportResponse:
    auth.require_scope(APIKeyScope.READ)
    assert auth.org_id is not None
    async with get_read_session() as session:
        return await preview_qa_report_core(
            session, experiment_id=experiment_id, org_id=auth.org_id
        )


@router.post("/experiments/{experiment_id}/qa/publish", response_model=QAReportResponse)
async def publish_experiment_qa_report(
    experiment_id: str,
    payload: QAReportPublishRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> QAReportResponse:
    assert auth.org_id is not None
    async with get_session() as session:
        response = await publish_qa_report_core(
            session,
            experiment_id=experiment_id,
            org_id=auth.org_id,
            published_by_user_id=auth.user_id,
            expected_draft_version=payload.expected_draft_version,
            expected_public_token=payload.expected_public_token,
        )
        await session.commit()
        return response


@router.post(
    "/experiments/{experiment_id}/qa/unpublish", response_model=QAReportResponse
)
async def unpublish_experiment_qa_report(
    experiment_id: str,
    payload: QAReportUnpublishRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> QAReportResponse:
    assert auth.org_id is not None
    async with get_session() as session:
        response = await unpublish_qa_report_core(
            session,
            experiment_id=experiment_id,
            org_id=auth.org_id,
            expected_draft_version=payload.expected_draft_version,
            expected_public_token=payload.expected_public_token,
        )
        await session.commit()
        return response

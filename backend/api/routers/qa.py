"""Custom, versioned AnalyzerBlock QA runs."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from auth import APIKeyScope, AuthContext, require_auth
from oddish.core.custom_qa import (
    get_custom_qa_run_core,
    run_custom_qa_core,
)
from oddish.db import get_session
from oddish.schemas import CustomQARunRequest, CustomQARunResponse

# Import registers the hosted Daytona backend with core's client factory.
from api.services.blocks.analyzer import sandbox_llm_client as _sandbox  # noqa: F401

router = APIRouter(tags=["QA"])


@router.get("/qa/runs/{run_id}", response_model=CustomQARunResponse)
async def get_run(run_id: str, auth: Annotated[AuthContext, Depends(require_auth)]) -> CustomQARunResponse:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        return await get_custom_qa_run_core(session, run_id=run_id, org_id=auth.org_id)


@router.post("/qa/runs", response_model=list[CustomQARunResponse])
async def run_qa(
    data: CustomQARunRequest,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_auth)],
    authorization: Annotated[str | None, Header()] = None,
) -> list[CustomQARunResponse]:
    auth.require_scope(APIKeyScope.TASKS)
    runtime_env = None
    if data.allow_credential_forwarding:
        token = (authorization or "").removeprefix("Bearer ").strip()
        if not token.startswith("ok_"):
            raise HTTPException(
                status_code=400,
                detail="Credential forwarding requires API-key authentication (ok_...), not a user JWT",
            )
        runtime_env = {
            "ODDISH_API_KEY": token,
            "ODDISH_API_URL": str(request.base_url).rstrip("/"),
        }
    async with get_session() as session:
        return await run_custom_qa_core(
            session, data=data, org_id=auth.org_id, user_id=auth.user_id,
            runtime_env=runtime_env,
        )

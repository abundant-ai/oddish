from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from auth import APIKeyScope, AuthContext, require_auth

from api.services.cc_chat.orchestrator import (
    CCChatOrchestrator,
    SessionNotFound,
)


router = APIRouter(tags=["CC Chat"])


_orchestrator_singleton: CCChatOrchestrator | None = None


def get_orchestrator() -> CCChatOrchestrator:
    """Dependency-style accessor; constructed in app startup (Task 11)."""
    if _orchestrator_singleton is None:
        raise RuntimeError(
            "CCChatOrchestrator not initialized; call init_orchestrator()"
        )
    return _orchestrator_singleton


def init_orchestrator(orch: CCChatOrchestrator) -> None:
    global _orchestrator_singleton
    _orchestrator_singleton = orch


class StartResponse(BaseModel):
    session_id: str


class SendMessageRequest(BaseModel):
    content: str


@router.post(
    "/api/experiments/{experiment_id}/cc-session",
    response_model=StartResponse,
)
async def start_session(
    experiment_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> StartResponse:
    auth.require_scope(APIKeyScope.READ)
    orch = get_orchestrator()
    sid = await orch.start(experiment_id=experiment_id, org_id=auth.org_id)
    return StartResponse(session_id=sid)


@router.post(
    "/api/experiments/{experiment_id}/cc-session/{session_id}/messages",
)
async def send_message(
    experiment_id: str,
    session_id: str,
    body: SendMessageRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> StreamingResponse:
    auth.require_scope(APIKeyScope.READ)
    orch = get_orchestrator()

    async def event_stream():
        try:
            async for event in orch.send(
                session_id=session_id, content=body.content
            ):
                kind = "error" if event.get("type") == "_stderr" else "message"
                yield f"event: {kind}\ndata: {json.dumps(event)}\n\n"
        except SessionNotFound:
            yield (
                'event: error\n'
                'data: {"type": "session_not_found"}\n\n'
            )
            return
        yield "event: done\ndata: {}\n\n"

    # We have to detect SessionNotFound up-front for the 404 status code,
    # because once we've returned StreamingResponse, the status is locked.
    state = orch._sessions.get(session_id)  # type: ignore[attr-defined]
    if state is None:
        raise HTTPException(status_code=404, detail="session_not_found")

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get(
    "/api/experiments/{experiment_id}/cc-session/{session_id}/skills.tar.gz",
)
async def export_skills(
    experiment_id: str,
    session_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> Response:
    auth.require_scope(APIKeyScope.READ)
    orch = get_orchestrator()
    state = orch._sessions.get(session_id)  # type: ignore[attr-defined]
    if state is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    try:
        blob = await orch.export_skills(session_id=session_id)
    except SessionNotFound:
        raise HTTPException(status_code=404, detail="session_not_found")
    return Response(
        content=blob,
        media_type="application/gzip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="cc-skills-{session_id}.tar.gz"'
            )
        },
    )


@router.delete(
    "/api/experiments/{experiment_id}/cc-session/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def close_session(
    experiment_id: str,
    session_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> Response:
    auth.require_scope(APIKeyScope.READ)
    orch = get_orchestrator()
    await orch.close(session_id=session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

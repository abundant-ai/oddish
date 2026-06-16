import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from auth import APIKeyScope, AuthContext, require_auth
from oddish.db import get_session
from models import ChatSession
from api.services.cc_chat import events as events_mod
from api.services.cc_chat.turns import running_turn
from api.services.cc_chat.orchestrator import ResumeUnavailable, SessionNotFound
from api.services.cc_chat.sessions_query import list_sessions

router = APIRouter(tags=["cc_chat"])


class ChatStartRequest(BaseModel):
    scope_kind: Literal["experiment", "task_probes", "task", "global"]
    scope_id: str


class ChatStartResponse(BaseModel):
    session_id: str


class ChatSendRequest(BaseModel):
    content: str


def _orch(request: Request):
    orch = getattr(request.app.state, "chat_orchestrator", None)
    if orch is None:
        raise HTTPException(503, detail="chat orchestrator unavailable")
    return orch


async def _require_session(session_id: str, org_id: str) -> ChatSession:
    async with get_session() as session:
        row = await session.get(ChatSession, session_id)
        if row is None or row.org_id != org_id:
            raise HTTPException(404, detail="session not found")
        return row


@router.post("/chat-sessions", response_model=ChatStartResponse)
async def start_session(
    body: ChatStartRequest,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> ChatStartResponse:
    auth.require_scope(APIKeyScope.READ)
    session_id = await _orch(request).start(
        org_id=auth.org_id,
        user_id=auth.user_id,
        scope_kind=body.scope_kind,
        scope_id=body.scope_id,
        db_session_factory=lambda: get_session(),
    )
    return ChatStartResponse(session_id=session_id)


@router.get("/chat-sessions")
async def list_sessions_route(
    auth: Annotated[AuthContext, Depends(require_auth)],
    scope_kind: Literal["experiment", "task_probes", "task", "global"],
    scope_id: str,
    limit: int = 10,
    offset: int = 0,
    q: str | None = None,
):
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        items, total = await list_sessions(
            session,
            org_id=auth.org_id,
            scope_kind=scope_kind,
            scope_id=scope_id,
            limit=limit,
            offset=offset,
            q=q,
        )
    return {"sessions": items, "total": total}


@router.get("/chat-sessions/{session_id}")
async def get_session_route(
    session_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
):
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        row = await session.get(ChatSession, session_id)
        if row is None or row.org_id != auth.org_id:
            raise HTTPException(404, detail="session not found")
        running = await running_turn(session, session_id=session_id)
        return {
            "session_id": row.id,
            "status": row.status,
            "scope_kind": row.scope_kind,
            "scope_id": row.scope_id,
            "running": running is not None,
            "created_at": row.created_at,
            "last_activity": row.last_activity,
            "closed_at": row.closed_at,
        }


@router.get("/chat-sessions/{session_id}/events")
async def replay_events(
    session_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
    since: int = -1,
):
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        row = await session.get(ChatSession, session_id)
        if row is None or row.org_id != auth.org_id:
            raise HTTPException(404, detail="session not found")
        return {"events": await events_mod.read_events(session, session_id=session_id, since=since)}


@router.post("/chat-sessions/{session_id}/messages")
async def send_message(
    session_id: str,
    body: ChatSendRequest,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_auth)],
):
    auth.require_scope(APIKeyScope.READ)
    await _require_session(session_id, auth.org_id)
    orch = _orch(request)

    async def event_stream():
        try:
            async for event in orch.send(
                session_id=session_id,
                content=body.content,
                db_session_factory=lambda: get_session(),
            ):
                # _stderr / internal events are informational; the only real
                # error signal in this stream is the explicit SessionNotFound
                # yield below. Stream everything else as a normal message event
                # (the frontend renders assistant text and ignores other types).
                yield f"event: message\ndata: {json.dumps(event)}\n\n"
        except SessionNotFound:
            yield 'event: error\ndata: {"type":"session_not_found"}\n\n'
            return
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.delete("/chat-sessions/{session_id}", status_code=204)
async def close_session(
    session_id: str,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_auth)],
):
    auth.require_scope(APIKeyScope.READ)
    await _require_session(session_id, auth.org_id)
    await _orch(request).close(session_id=session_id, db_session_factory=lambda: get_session())


@router.post("/chat-sessions/{session_id}/resume", status_code=204)
async def resume_session(
    session_id: str,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_auth)],
):
    auth.require_scope(APIKeyScope.READ)
    await _require_session(session_id, auth.org_id)
    try:
        await _orch(request).resume(session_id=session_id, db_session_factory=lambda: get_session())
    except ResumeUnavailable:
        raise HTTPException(409, detail="This chat can't be restored (no saved session).")
    except SessionNotFound:
        raise HTTPException(404, detail="session not found")

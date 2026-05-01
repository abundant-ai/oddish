from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse

from auth import APIKeyScope, AuthContext, require_auth
from api.services.agent_sandbox_client import AgentSandboxClient

router = APIRouter(tags=["CC Chat"])


def _client_from_app(request: Request) -> AgentSandboxClient:
    c = getattr(request.app.state, "agent_sandbox_client", None)
    if c is None:
        raise HTTPException(
            503,
            detail={
                "error": {
                    "code": "not_ready",
                    "message": "agent-sandbox-service not configured",
                }
            },
        )
    return c


# --- Start session ---
@router.post("/api/experiments/{experiment_id}/cc-session")
async def start_experiment_session(
    experiment_id: str,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    auth.require_scope(APIKeyScope.READ)
    client = _client_from_app(request)
    sid = await client.start_chat_session(
        org_id=auth.org_id,
        user_id=getattr(auth, "user_id", None),
        scope={"kind": "experiment", "experiment_id": experiment_id},
    )
    return {"session_id": sid}


@router.post("/api/tasks/{task_id}/cc-session")
async def start_task_session(
    task_id: str,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    auth.require_scope(APIKeyScope.READ)
    client = _client_from_app(request)
    sid = await client.start_chat_session(
        org_id=auth.org_id,
        user_id=getattr(auth, "user_id", None),
        scope={"kind": "task_probes", "task_id": task_id},
    )
    return {"session_id": sid}


# --- Send message (SSE pass-through) ---
def _make_sse_proxy(
    client: AgentSandboxClient, session_id: str, content: str
) -> StreamingResponse:
    async def gen():
        async for chunk in client.stream_chat_message(
            session_id=session_id, content=content
        ):
            yield chunk

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/api/experiments/{experiment_id}/cc-session/{session_id}/messages")
async def send_experiment_message(
    experiment_id: str,
    session_id: str,
    body: dict,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_auth)],
):
    auth.require_scope(APIKeyScope.READ)
    return _make_sse_proxy(_client_from_app(request), session_id, body["content"])


@router.post("/api/tasks/{task_id}/cc-session/{session_id}/messages")
async def send_task_message(
    task_id: str,
    session_id: str,
    body: dict,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_auth)],
):
    auth.require_scope(APIKeyScope.READ)
    return _make_sse_proxy(_client_from_app(request), session_id, body["content"])


# --- Close session ---
@router.delete(
    "/api/experiments/{experiment_id}/cc-session/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def close_experiment_session(
    experiment_id: str,
    session_id: str,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> Response:
    auth.require_scope(APIKeyScope.READ)
    await _client_from_app(request).close_chat_session(session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/api/tasks/{task_id}/cc-session/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def close_task_session(
    task_id: str,
    session_id: str,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> Response:
    auth.require_scope(APIKeyScope.READ)
    await _client_from_app(request).close_chat_session(session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Export skills ---
@router.get("/api/experiments/{experiment_id}/cc-session/{session_id}/skills.tar.gz")
async def export_experiment_skills(
    experiment_id: str,
    session_id: str,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> Response:
    auth.require_scope(APIKeyScope.READ)
    blob = await _client_from_app(request).export_chat_skills(session_id)
    return Response(
        content=blob,
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="cc-skills-{session_id}.tar.gz"'
        },
    )


@router.get("/api/tasks/{task_id}/cc-session/{session_id}/skills.tar.gz")
async def export_task_skills(
    task_id: str,
    session_id: str,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> Response:
    auth.require_scope(APIKeyScope.READ)
    blob = await _client_from_app(request).export_chat_skills(session_id)
    return Response(
        content=blob,
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="cc-skills-{session_id}.tar.gz"'
        },
    )

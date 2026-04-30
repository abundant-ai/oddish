from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import and_, select

from auth import APIKeyScope, AuthContext, require_auth

from api.services.cc_chat.orchestrator import (
    CCChatOrchestrator,
    SessionNotFound,
)
from oddish.db import TaskModel, TrialModel, get_session


logger = logging.getLogger("cc_chat.router")


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


# ---------------------------------------------------------------------------
# Task-scoped cc-chat: chat across all probe runs of a single task, with the
# task definition (task.toml, instruction.md, verifier/) uploaded alongside.
# Useful for cheat investigations — the verifier source is the most important
# input for judging whether a passing reward came from solving the task or
# from gaming the verifier.
#
# Send / delete / skills routes mirror the experiment-scoped ones because
# session_id is the only identifier the orchestrator needs; they're aliased
# at the /tasks/... path so the frontend's URL space stays parallel.
# ---------------------------------------------------------------------------

# Files inside the task source dir we never want to upload (heavy, irrelevant
# for cheat reasoning, or both).
_TASK_DEF_SKIP_DIRS = {".git", "node_modules", "__pycache__", "dist", "build", "target"}


_PRIORITY_TASK_FILES = ("task.toml", "instruction.md")
_PRIORITY_TASK_DIRS = ("verifier",)


def _iter_task_definition(
    task_path: Path,
) -> list[tuple[str, bytes]]:
    """Walk the task source dir and return (workspace_rel_path, bytes).

    Always includes task.toml, instruction.md, and the entire verifier/
    subtree regardless of size — those are the files claude needs for
    cheat investigations. Then fills remaining quota with other files
    under a per-file size limit. Skips dot-dirs and known heavy build
    trees. Total cap ~10 MB for the discretionary tail.

    Per-file cap is critical: long-horizon tasks ship multi-MB
    `environment/golden.jsonl` test corpora that aren't useful for the
    chat and previously caused the cumulative cap to bail on the very
    first file (alphabetical sort), starving task.toml/instruction.md.
    """
    if not task_path.is_dir():
        return []

    out: list[tuple[str, bytes]] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        if path in seen or not path.is_file():
            return
        rel_parts = path.relative_to(task_path).parts
        if any(p in _TASK_DEF_SKIP_DIRS or p.startswith(".") for p in rel_parts):
            return
        try:
            content = path.read_bytes()
        except OSError:
            return
        rel = "/".join(("task", *rel_parts))
        out.append((rel, content))
        seen.add(path)

    # Phase 1: priority files — always include.
    for name in _PRIORITY_TASK_FILES:
        add(task_path / name)
    for sub in _PRIORITY_TASK_DIRS:
        sub_path = task_path / sub
        if sub_path.is_dir():
            for path in sorted(sub_path.rglob("*")):
                add(path)

    # Phase 2: discretionary tail — capped per-file and overall.
    per_file_cap = 1 * 1024 * 1024
    total_cap = 10 * 1024 * 1024
    discretionary_total = 0
    for path in sorted(task_path.rglob("*")):
        if path in seen or not path.is_file():
            continue
        rel_parts = path.relative_to(task_path).parts
        if any(p in _TASK_DEF_SKIP_DIRS or p.startswith(".") for p in rel_parts):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > per_file_cap:
            logger.info(
                "skipping large task file %s (%.1f MB > %.1f MB per-file cap)",
                path.relative_to(task_path),
                size / 1e6,
                per_file_cap / 1e6,
            )
            continue
        if discretionary_total + size > total_cap:
            logger.info(
                "task definition discretionary cap %dMB hit; remainder skipped",
                total_cap // (1024 * 1024),
            )
            break
        try:
            content = path.read_bytes()
        except OSError:
            continue
        rel = "/".join(("task", *rel_parts))
        out.append((rel, content))
        seen.add(path)
        discretionary_total += len(content)

    return out


def _iter_trial_dir(
    trial_root: Path, trial_id: str
) -> list[tuple[str, bytes]]:
    """Walk a trial result dir and return files keyed under jobs/<trial_id>/."""
    if not trial_root.is_dir():
        return []
    out: list[tuple[str, bytes]] = []
    for path in trial_root.rglob("*", recurse_symlinks=True):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(trial_root).parts
        if any(p.startswith(".") for p in rel_parts):
            continue
        try:
            content = path.read_bytes()
        except OSError:
            continue
        rel = "/".join(("jobs", trial_id, *rel_parts))
        out.append((rel, content))
    return out


@router.post(
    "/api/tasks/{task_id}/cc-session",
    response_model=StartResponse,
)
async def start_task_session(
    task_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> StartResponse:
    auth.require_scope(APIKeyScope.READ)
    orch = get_orchestrator()

    async with get_session() as session:
        task = (
            await session.execute(
                select(TaskModel).where(TaskModel.id == task_id)
            )
        ).scalar_one_or_none()
        if task is None:
            raise HTTPException(status_code=404, detail="task_not_found")
        if task.org_id is not None and task.org_id != auth.org_id:
            raise HTTPException(status_code=403, detail="org_mismatch")

        trials_q = (
            select(TrialModel)
            .where(TrialModel.task_id == task_id)
            .order_by(TrialModel.started_at.desc())
        )
        trials = (await session.execute(trials_q)).scalars().all()

    # Filter to probe trials only (harbor_config.mode == "probe"). Skip
    # trials with no harbor_result_path on disk — nothing to upload.
    probe_trials = [
        t for t in trials
        if (t.harbor_config or {}).get("mode") == "probe"
        and t.harbor_result_path
    ]
    if not probe_trials:
        raise HTTPException(
            status_code=400,
            detail="no_probe_trials_with_artifacts",
        )

    files: list[tuple[str, bytes]] = []
    trial_ids: list[str] = []
    for trial in probe_trials:
        trial_root = Path(trial.harbor_result_path)
        per_trial = _iter_trial_dir(trial_root, trial.id)
        if not per_trial:
            continue
        files.extend(per_trial)
        trial_ids.append(trial.id)

    # Resolve the task source directory and upload its definition.
    if task.task_path:
        task_path = Path(task.task_path)
        if not task_path.is_absolute():
            task_path = Path.home() / task.task_path
        files.extend(_iter_task_definition(task_path))

    sid = await orch.start_task_probes(
        task_id=task_id,
        task_name=task.name,
        org_id=auth.org_id,
        files=files,
        trial_ids=trial_ids,
    )
    return StartResponse(session_id=sid)


@router.post(
    "/api/tasks/{task_id}/cc-session/{session_id}/messages",
)
async def send_task_message(
    task_id: str,
    session_id: str,
    body: SendMessageRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> StreamingResponse:
    # Same body as send_message — session_id is the only identifier the
    # orchestrator needs; task_id is just for URL parity with the FE.
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

    state = orch._sessions.get(session_id)  # type: ignore[attr-defined]
    if state is None:
        raise HTTPException(status_code=404, detail="session_not_found")

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.delete(
    "/api/tasks/{task_id}/cc-session/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def close_task_session(
    task_id: str,
    session_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> Response:
    auth.require_scope(APIKeyScope.READ)
    orch = get_orchestrator()
    await orch.close(session_id=session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/api/tasks/{task_id}/cc-session/{session_id}/skills.tar.gz",
)
async def export_task_skills(
    task_id: str,
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

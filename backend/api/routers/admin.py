"""Admin endpoints — auth wrapper over oddish core diagnostics."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from auth import AuthContext, require_admin
from oddish.api.admin import (
    QueueSlotsResponse,
    QueueStatusResponse,
    OrphanedStateResponse,
    get_queue_slots_core,
    get_queue_status_core,
    get_orphaned_state_core,
)
from oddish.db import get_session

router = APIRouter(prefix="/admin", tags=["Admin"])


@router.get("/slots", response_model=QueueSlotsResponse)
async def get_queue_slots(
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> QueueSlotsResponse:
    """Get current state of queue-key slot leases."""
    async with get_session() as session:
        return await get_queue_slots_core(session)


@router.get("/queue-status", response_model=QueueStatusResponse)
async def get_queue_status(
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> QueueStatusResponse:
    """Get queue status from the trials/tasks tables (the source of truth)."""
    async with get_session() as session:
        return await get_queue_status_core(session)


@router.get("/orphaned-state", response_model=OrphanedStateResponse)
async def get_orphaned_state(
    auth: Annotated[AuthContext, Depends(require_admin)],
    stale_after_minutes: int = Query(10, ge=1, le=240),
) -> OrphanedStateResponse:
    """Summarize stale queue/pipeline state."""
    async with get_session() as session:
        return await get_orphaned_state_core(
            session, stale_after_minutes=stale_after_minutes
        )

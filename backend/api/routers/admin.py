"""
Admin endpoints for internal system monitoring.

Provides visibility into:
- Queue slot leases (worker concurrency tracking)
- PGQueuer job queue state (raw queue internals)
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text

from auth import AuthContext, require_admin
from oddish.config import settings
from oddish.db import get_session, utcnow

router = APIRouter(prefix="/admin", tags=["Admin"])


# =============================================================================
# Response Models
# =============================================================================


class QueueSlot(BaseModel):
    """A single queue-key slot lease."""

    queue_key: str
    slot: int
    locked_by: str | None
    locked_until: datetime | None
    is_active: bool  # True if currently locked


class QueueSlotSummary(BaseModel):
    """Summary of slots for a single queue key."""

    queue_key: str
    total_slots: int
    active_slots: int
    slots: list[QueueSlot]


class QueueSlotsResponse(BaseModel):
    """Response for queue slot endpoint."""

    queue_keys: list[QueueSlotSummary]
    total_slots: int
    total_active: int
    timestamp: str


class PGQueuerJob(BaseModel):
    """A single job from the pgqueuer table."""

    id: int
    priority: int
    created: datetime | None
    updated: datetime | None
    status: str
    entrypoint: str
    payload: dict | None


class PGQueuerStats(BaseModel):
    """Statistics for pgqueuer jobs."""

    total: int
    by_status: dict[str, int]
    by_entrypoint: dict[str, dict[str, int]]


class PGQueuerResponse(BaseModel):
    """Response for pgqueuer endpoint."""

    jobs: list[PGQueuerJob]
    stats: PGQueuerStats
    page: int
    page_size: int
    has_more: bool
    timestamp: str


# =============================================================================
# Endpoints
# =============================================================================


@router.get("/slots", response_model=QueueSlotsResponse)
async def get_queue_slots(
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> QueueSlotsResponse:
    """
    Get current state of queue-key slot leases.

    Shows which workers have acquired slots and when they expire.
    Requires admin role.
    """
    now = utcnow()

    async with get_session() as session:
        result = await session.execute(
            text(
                """
                SELECT queue_key, slot, locked_by, locked_until
                FROM queue_slots
                ORDER BY queue_key, slot
                """
            )
        )
        rows = result.all()

    # Group by canonical queue key
    queue_map: dict[str, list[QueueSlot]] = {}
    for row in rows:
        queue_key = settings.normalize_queue_key(row[0])
        slot = QueueSlot(
            queue_key=queue_key,
            slot=row[1],
            locked_by=row[2],
            locked_until=row[3],
            is_active=row[2] is not None and row[3] is not None and row[3] > now,
        )
        if queue_key not in queue_map:
            queue_map[queue_key] = []
        queue_map[queue_key].append(slot)

    # Build summaries
    queue_keys = []
    total_slots = 0
    total_active = 0

    for queue_key, slots in sorted(queue_map.items()):
        active_count = sum(1 for s in slots if s.is_active)
        queue_keys.append(
            QueueSlotSummary(
                queue_key=queue_key,
                total_slots=len(slots),
                active_slots=active_count,
                slots=slots,
            )
        )
        total_slots += len(slots)
        total_active += active_count

    return QueueSlotsResponse(
        queue_keys=queue_keys,
        total_slots=total_slots,
        total_active=total_active,
        timestamp=now.isoformat(),
    )


@router.get("/pgqueuer", response_model=PGQueuerResponse)
async def get_pgqueuer_jobs(
    auth: Annotated[AuthContext, Depends(require_admin)],
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    status: str | None = Query(
        None, description="Filter by status (queued, picked, etc)"
    ),
    entrypoint: str | None = Query(None, description="Filter by queue entrypoint"),
) -> PGQueuerResponse:
    """
    Get raw pgqueuer job queue state.

    Shows the internal job queue used by workers. This is the source of truth
    for queue state before jobs become trials.

    Requires admin role.
    """
    import json

    now = utcnow()
    offset = (page - 1) * page_size

    async with get_session() as session:
        # Build filters
        filters = []
        params: dict = {"limit": page_size + 1, "offset": offset}

        if status:
            filters.append("status::text = :status")
            params["status"] = status
        if entrypoint:
            filters.append("entrypoint = :entrypoint")
            params["entrypoint"] = entrypoint

        where_clause = " AND ".join(filters) if filters else "1=1"

        # Fetch jobs (one extra to check if there are more)
        result = await session.execute(
            text(
                f"""
                SELECT id, priority, created, updated, status::text, entrypoint, payload
                FROM pgqueuer
                WHERE {where_clause}
                ORDER BY created DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        )
        rows = result.all()

        # Parse jobs
        jobs = []
        for row in rows[:page_size]:
            payload_raw = row[6]
            payload_dict = None
            if payload_raw:
                try:
                    if isinstance(payload_raw, bytes):
                        payload_dict = json.loads(
                            payload_raw.decode("utf-8", errors="replace")
                        )
                    elif isinstance(payload_raw, str):
                        payload_dict = json.loads(payload_raw)
                    else:
                        payload_dict = payload_raw
                except Exception:
                    payload_dict = {"raw": str(payload_raw)[:200]}

            jobs.append(
                PGQueuerJob(
                    id=row[0],
                    priority=row[1],
                    created=row[2],
                    updated=row[3],
                    status=row[4],
                    entrypoint=row[5],
                    payload=payload_dict,
                )
            )

        has_more = len(rows) > page_size

        # Get stats (unfiltered for overview)
        stats_result = await session.execute(
            text(
                """
                SELECT
                    status::text,
                    entrypoint,
                    COUNT(*) as count
                FROM pgqueuer
                GROUP BY status, entrypoint
                """
            )
        )
        stats_rows = stats_result.all()

        by_status: dict[str, int] = {}
        by_entrypoint: dict[str, dict[str, int]] = {}

        for status_val, ep, count in stats_rows:
            by_status[status_val] = by_status.get(status_val, 0) + count
            if ep not in by_entrypoint:
                by_entrypoint[ep] = {}
            by_entrypoint[ep][status_val] = count

        total = sum(by_status.values())

    return PGQueuerResponse(
        jobs=jobs,
        stats=PGQueuerStats(
            total=total,
            by_status=by_status,
            by_entrypoint=by_entrypoint,
        ),
        page=page,
        page_size=page_size,
        has_more=has_more,
        timestamp=now.isoformat(),
    )

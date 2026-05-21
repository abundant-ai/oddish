"""Enqueue surface for the unified ``worker_jobs`` table.

``enqueue_worker_job`` is the only blessed way to insert a new row.
Callers pass an ``EnqueueRequest`` describing the work; the helper
generates the id, sets defaults, runs the kind's ``validate_payload``
hook (when a handler is registered for that kind), and flushes the
session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from oddish.db import (
    WorkerJobKind,
    WorkerJobModel,
    WorkerJobStatus,
    generate_id,
    utcnow,
)
from oddish.workers.queue.dispatch_signal import notify_dispatch


@dataclass
class EnqueueRequest:
    """Payload describing a single row to be enqueued on ``worker_jobs``."""

    kind: WorkerJobKind
    queue_key: str
    payload: dict[str, Any] = field(default_factory=dict)
    subject_table: str | None = None
    subject_id: str | None = None
    priority: int = 0
    max_attempts: int = 6
    org_id: str | None = None
    parent_job_id: str | None = None


async def enqueue_worker_job(
    session,
    request: EnqueueRequest,
    *,
    validate: bool = True,
) -> WorkerJobModel:
    """Insert a ``worker_jobs`` row inside the caller's session.

    The ``session`` parameter is untyped so the helper works with both a
    real SQLAlchemy ``AsyncSession`` and the lightweight fake sessions
    used in tests -- both need ``add`` and ``flush``.
    """
    payload = dict(request.payload or {})
    if validate:
        try:
            from oddish.workers.jobs.registry import get_handler

            handler = get_handler(request.kind)
        except Exception:
            handler = None
        if handler is not None:
            payload = handler.validate_payload(payload)

    row = WorkerJobModel(
        id=generate_id(),
        kind=request.kind,
        status=WorkerJobStatus.QUEUED,
        queue_key=request.queue_key,
        priority=request.priority,
        subject_table=request.subject_table,
        subject_id=request.subject_id,
        parent_job_id=request.parent_job_id,
        payload=payload,
        attempts=0,
        max_attempts=request.max_attempts,
        available_after=utcnow(),
        org_id=request.org_id,
    )
    session.add(row)
    await session.flush()
    # Best-effort wake of the dispatch reactor. Fires on flush rather
    # than commit, so a rolled-back transaction can cause a spurious
    # wake -- but the reactor handles "wake, find nothing, sleep" as a
    # no-op, and a dropped wake (e.g. transport failure) is caught by
    # the safety-net poll. The trade-off favours simplicity over
    # serialising the wake to commit time.
    await notify_dispatch(row.queue_key)
    return row


__all__ = ["EnqueueRequest", "enqueue_worker_job"]

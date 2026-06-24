"""Enqueue surface for the unified ``worker_jobs`` table.

``enqueue_worker_job`` is the only blessed way to insert a new row.
Callers pass an ``EnqueueRequest`` describing the work; the helper
generates the id, sets defaults, runs the kind's ``validate_payload``
hook (when a handler is registered for that kind), and flushes the
session.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from oddish.db import (
    WorkerJobKind,
    WorkerJobModel,
    WorkerJobStatus,
    generate_id,
    utcnow,
)


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
    # Harbor execution variant ('default' | '<registry-id>' | 'ephemeral'). Part
    # of the effective dispatch key: the dispatcher counts/spawns per
    # (queue_key, harbor_variant_id) and the claim is scoped to it.
    harbor_variant_id: str = "default"


def _validated_payload(request: EnqueueRequest, validate: bool) -> dict[str, Any]:
    """Resolve a request's payload, running the kind's validate_payload hook."""
    payload = dict(request.payload or {})
    if validate:
        try:
            from oddish.workers.jobs.registry import get_handler

            handler = get_handler(request.kind)
        except Exception:
            handler = None
        if handler is not None:
            payload = handler.validate_payload(payload)
    return payload


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
    payload = _validated_payload(request, validate)

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
        harbor_variant_id=request.harbor_variant_id,
    )
    session.add(row)
    await session.flush()
    _wake_dispatcher()
    return row


def _wake_dispatcher() -> None:
    """Best-effort in-app wake on enqueue (no-op when no loop runs in-process).

    A same-process fast path over the fallback poll; cross-process hosts
    (Modal/Docker multi-container) rely on the fallback poll / cron. Never
    raises into the enqueue path.
    """
    try:
        from oddish.dispatch.cycle import signal_dispatch

        signal_dispatch()
    except Exception:
        pass


# One parameterized INSERT for an arbitrary number of rows. ``unnest`` keeps
# the statement shape identical regardless of row count -- important under
# Supavisor transaction pooling + ``statement_cache_size=0`` (asyncpg 0.31
# anonymous statements), where a multi-row VALUES would re-Parse a distinct
# statement per N. Every array parameter is explicitly cast so asyncpg
# resolves types without a round-trip. Columns omitted here (``status``,
# ``priority``, ``attempts``, ``available_after``, ...) fall to their DB
# defaults, matching ``enqueue_worker_job``.
_BULK_INSERT_WORKER_JOBS_SQL = text(
    """
    INSERT INTO worker_jobs
        (id, kind, status, queue_key, priority, subject_table, subject_id,
         parent_job_id, payload, attempts, max_attempts, available_after,
         org_id, harbor_variant_id, created_at, updated_at)
    SELECT
        j.id,
        j.kind::worker_job_kind,
        'QUEUED'::worker_job_status,
        j.queue_key,
        j.priority,
        j.subject_table,
        j.subject_id,
        j.parent_job_id,
        j.payload::jsonb,
        0,
        j.max_attempts,
        NOW(),
        j.org_id,
        j.harbor_variant_id,
        NOW(),
        NOW()
    FROM unnest(
        CAST(:id AS text[]),
        CAST(:kind AS text[]),
        CAST(:queue_key AS text[]),
        CAST(:priority AS int[]),
        CAST(:subject_table AS text[]),
        CAST(:subject_id AS text[]),
        CAST(:parent_job_id AS text[]),
        CAST(:payload AS text[]),
        CAST(:max_attempts AS int[]),
        CAST(:org_id AS text[]),
        CAST(:harbor_variant_id AS text[])
    ) WITH ORDINALITY AS j(
        id, kind, queue_key, priority, subject_table, subject_id,
        parent_job_id, payload, max_attempts, org_id, harbor_variant_id, ord
    )
    """
)


async def bulk_enqueue_worker_jobs(
    session,
    requests: list[EnqueueRequest],
    *,
    validate: bool = True,
) -> list[str]:
    """Insert many ``worker_jobs`` rows with a single ``INSERT ... unnest``.

    Behaviorally equivalent to calling :func:`enqueue_worker_job` once per
    request (same id generation, ``validate_payload`` hook, and column
    defaults), but collapses N per-row ``add``+``flush`` round-trips into one
    parameterized statement. JSON payloads are pre-serialized to text and
    cast back to ``jsonb`` (passing a list of dicts is an asyncpg array trap).
    Returns the generated row ids in input order.
    """
    if not requests:
        return []

    ids = [generate_id() for _ in requests]
    params = {
        "id": ids,
        "kind": [r.kind.value for r in requests],
        "queue_key": [r.queue_key for r in requests],
        "priority": [r.priority for r in requests],
        "subject_table": [r.subject_table for r in requests],
        "subject_id": [r.subject_id for r in requests],
        "parent_job_id": [r.parent_job_id for r in requests],
        "payload": [json.dumps(_validated_payload(r, validate)) for r in requests],
        "max_attempts": [r.max_attempts for r in requests],
        "org_id": [r.org_id for r in requests],
        "harbor_variant_id": [r.harbor_variant_id for r in requests],
    }
    await session.execute(_BULK_INSERT_WORKER_JOBS_SQL, params)
    _wake_dispatcher()
    return ids


__all__ = [
    "EnqueueRequest",
    "bulk_enqueue_worker_jobs",
    "enqueue_worker_job",
]

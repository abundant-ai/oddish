"""SQL execution helpers that take an open connection.

Used by every ``DBProxy`` implementation -- direct asyncpg, pooled
asyncpg, and the Modal-backed service -- so the queries themselves
live in exactly one place.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg

from oddish.db import WorkerJobKind


_CLAIM_WORKER_JOB_SQL = """
UPDATE worker_jobs
SET    status = 'RUNNING',
       claimed_at = NOW(),
       heartbeat_at = NOW(),
       attempts = attempts + 1,
       current_worker_id = $2,
       current_queue_slot = $3,
       modal_function_call_id = $4,
       started_at = COALESCE(started_at, NOW()),
       finished_at = NULL,
       next_retry_at = NULL,
       error_message = NULL
WHERE  id = (
    SELECT wj.id
    FROM   worker_jobs wj
    LEFT JOIN trials tr
        ON  wj.kind::text = 'TRIAL'
        AND wj.subject_table = 'trials'
        AND wj.subject_id = tr.id
    LEFT JOIN tasks tk ON tr.task_id = tk.id
    LEFT JOIN (
        SELECT COALESCE(tk2.created_by_user_id, tk2.user) AS fairness_key,
               COUNT(*) AS running_count
        FROM   worker_jobs wj2
        JOIN   trials tr2  ON wj2.subject_id = tr2.id
        JOIN   tasks  tk2  ON tr2.task_id = tk2.id
        WHERE  wj2.kind::text = 'TRIAL'
          AND  wj2.status::text = 'RUNNING'
          AND  wj2.queue_key = $1
          AND  tr2.deleted_at IS NULL
          AND  tk2.deleted_at IS NULL
        GROUP  BY COALESCE(tk2.created_by_user_id, tk2.user)
    ) rpg ON rpg.fairness_key = COALESCE(tk.created_by_user_id, tk.user)
    WHERE  wj.queue_key = $1
      AND  wj.status::text IN ('QUEUED', 'RETRYING')
      AND  wj.available_after <= NOW()
      AND  (tr.deleted_at IS NULL)
      AND  (tk.deleted_at IS NULL)
    ORDER  BY wj.priority DESC,
              COALESCE(rpg.running_count, 0) ASC,
              wj.created_at ASC
    LIMIT  1
    FOR    UPDATE OF wj SKIP LOCKED
)
RETURNING id, kind::text AS kind, subject_table, subject_id, payload,
          attempts, max_attempts, queue_key, org_id, parent_job_id;
"""


@dataclass(frozen=True)
class ClaimedJobRow:
    id: str
    kind: WorkerJobKind
    queue_key: str
    subject_table: str | None
    subject_id: str | None
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    org_id: str | None
    parent_job_id: str | None


async def claim_job(
    conn: asyncpg.Connection,
    *,
    queue_key: str,
    worker_id: str,
    queue_slot: int | None,
    modal_function_call_id: str | None,
) -> ClaimedJobRow | None:
    row = await conn.fetchrow(
        _CLAIM_WORKER_JOB_SQL,
        queue_key,
        worker_id,
        queue_slot,
        modal_function_call_id,
    )
    if row is None:
        return None

    raw_payload = row["payload"]
    if isinstance(raw_payload, str):
        payload = json.loads(raw_payload) if raw_payload else {}
    else:
        payload = dict(raw_payload or {})

    return ClaimedJobRow(
        id=str(row["id"]),
        kind=WorkerJobKind(row["kind"]),
        queue_key=str(row["queue_key"]),
        subject_table=row["subject_table"],
        subject_id=row["subject_id"],
        payload=payload,
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        org_id=row["org_id"],
        parent_job_id=row["parent_job_id"],
    )


async def heartbeat(
    conn: asyncpg.Connection,
    *,
    job_id: str,
    pending_failure_count: int,
    pending_last_error: str | None,
) -> None:
    if pending_failure_count > 0:
        await conn.execute(
            """
            UPDATE worker_jobs
            SET    heartbeat_at = NOW(),
                   heartbeat_failure_count = heartbeat_failure_count + $2,
                   last_heartbeat_error = $3,
                   last_heartbeat_error_at = NOW()
            WHERE  id = $1
              AND  status::text = 'RUNNING'
            """,
            job_id,
            pending_failure_count,
            (pending_last_error or "")[:500] or None,
        )
    else:
        await conn.execute(
            """
            UPDATE worker_jobs
            SET    heartbeat_at = NOW()
            WHERE  id = $1
              AND  status::text = 'RUNNING'
            """,
            job_id,
        )


async def record_success(
    conn: asyncpg.Connection,
    *,
    job_id: str,
    summary_json: str | None,
) -> None:
    await conn.execute(
        """
        UPDATE worker_jobs
        SET    status = 'SUCCESS',
               result_summary = $2::jsonb,
               finished_at = NOW(),
               heartbeat_at = NOW(),
               next_retry_at = NULL,
               error_message = NULL
        WHERE  id = $1
        """,
        job_id,
        summary_json,
    )


async def record_retry(
    conn: asyncpg.Connection,
    *,
    job_id: str,
    error_message: str | None,
    retry_at: datetime | None,
    mirror_to_trials: bool,
    subject_id: str | None,
) -> None:
    await conn.execute(
        """
        UPDATE worker_jobs
        SET    status = 'RETRYING',
               error_message = $2,
               next_retry_at = $3,
               available_after = COALESCE($3::timestamptz, NOW()),
               current_worker_id = NULL,
               current_queue_slot = NULL,
               modal_function_call_id = NULL
        WHERE  id = $1
        """,
        job_id,
        error_message,
        retry_at,
    )
    if mirror_to_trials and subject_id and retry_at is not None:
        await conn.execute(
            """
            UPDATE trials
            SET    status = 'RETRYING',
                   error_message = $2,
                   next_retry_at = $3,
                   current_worker_id = NULL,
                   current_queue_slot = NULL,
                   heartbeat_at = NOW()
            WHERE  id = $1
              AND  deleted_at IS NULL
            """,
            subject_id,
            error_message,
            retry_at,
        )


async def record_failure(
    conn: asyncpg.Connection,
    *,
    job_id: str,
    error_message: str | None,
) -> None:
    await conn.execute(
        """
        UPDATE worker_jobs
        SET    status = 'FAILED',
               error_message = $2,
               finished_at = NOW(),
               next_retry_at = NULL
        WHERE  id = $1
        """,
        job_id,
        error_message,
    )


async def ensure_slots(
    conn: asyncpg.Connection, *, queue_key: str, limit: int
) -> None:
    await conn.execute(
        """
        INSERT INTO queue_slots (queue_key, slot)
        SELECT $1, slot
        FROM generate_series(0, $2 - 1) AS slot
        ON CONFLICT DO NOTHING
        """,
        queue_key,
        limit,
    )


async def acquire_slot(
    conn: asyncpg.Connection,
    *,
    queue_key: str,
    limit: int,
    worker_id: str,
    lease_seconds: int,
) -> int | None:
    async with conn.transaction():
        row = await conn.fetchrow(
            """
            WITH candidate AS (
                SELECT queue_key, slot
                FROM queue_slots
                WHERE queue_key = $1
                  AND slot < $2
                  AND (locked_until IS NULL OR locked_until <= NOW())
                ORDER BY slot
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE queue_slots
            SET locked_by = $3,
                locked_until = NOW() + make_interval(secs => $4)
            FROM candidate
            WHERE queue_slots.queue_key = candidate.queue_key
              AND queue_slots.slot = candidate.slot
            RETURNING queue_slots.slot
            """,
            queue_key,
            limit,
            worker_id,
            lease_seconds,
        )
    return None if row is None else int(row["slot"])


async def release_slot(
    conn: asyncpg.Connection,
    *,
    queue_key: str,
    slot: int,
    worker_id: str,
) -> None:
    await conn.execute(
        """
        UPDATE queue_slots
        SET locked_by = NULL,
            locked_until = NULL
        WHERE queue_key = $1
          AND slot = $2
          AND locked_by = $3
        """,
        queue_key,
        slot,
        worker_id,
    )


async def cleanup_stale_slots(conn: asyncpg.Connection) -> int:
    result = await conn.execute(
        """
        UPDATE queue_slots
        SET locked_by = NULL,
            locked_until = NULL
        WHERE locked_by IS NOT NULL
          AND locked_until IS NOT NULL
          AND locked_until <= NOW()
        """
    )
    try:
        return int(str(result).split()[-1])
    except Exception:
        return 0

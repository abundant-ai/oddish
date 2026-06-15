"""Enqueue surface for the TAG_PROJECT worker kind.

Every tag write site (apply / unapply / merge / archive / exclude /
unexclude / membership change / new version) calls this helper in the
same transaction as the truth write. The handler recomputes the
projection arrays from truth, so duplicate enqueues are harmless
*correctness-wise* — but enqueuing N redundant jobs wastes worker
cycles, so we **coalesce at the DB layer** via the partial-unique index
``uq_worker_jobs_tag_project_active``.

The index is unique on ``(kind, subject_table, subject_id)`` with a
``status IN ('QUEUED','RETRYING')`` predicate. We INSERT with
``ON CONFLICT (kind, subject_table, subject_id) WHERE <that predicate>
DO NOTHING`` — index inference, because the coalescing target is a
partial unique INDEX, which Postgres cannot reference via
``ON CONSTRAINT`` — so concurrent writers collapse onto a single
in-flight job. When the
dispatcher moves the job to ``RUNNING``/``SUCCESS``/``FAILED`` it leaves
the partial index and a fresh enqueue is allowed (so a write that lands
during a recompute still gets its own follow-up).

Continuation jobs from the chunked fan-out handler carry an
``after_task_id`` cursor in the payload.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text


TAG_PROJECT_QUEUE_KEY = "tag-project"


async def _insert_tag_project_with_coalescing(
    session,
    *,
    kind: str,
    queue_key: str,
    payload: dict,
    subject_table: str,
    subject_id: str | None,
    org_id: str | None,
) -> dict | None:
    """Atomic insert-or-coalesce. Returns the inserted row's id, or
    ``None`` if another in-flight row already covers this (kind,
    subject_table, subject_id) tuple.
    """
    new_id = str(uuid.uuid4())
    rows = (
        await session.execute(
            text(
                """
                INSERT INTO worker_jobs (
                    id, kind, queue_key, status, payload,
                    subject_table, subject_id, org_id,
                    created_at, updated_at
                )
                VALUES (
                    :id, CAST(:kind AS worker_job_kind), :queue_key,
                    'QUEUED',
                    CAST(:payload AS JSONB),
                    :subject_table, :subject_id, :org_id,
                    NOW(), NOW()
                )
                ON CONFLICT (kind, subject_table, subject_id)
                    WHERE kind = 'TAG_PROJECT'
                      AND status IN ('QUEUED', 'RETRYING')
                      AND subject_id IS NOT NULL
                    DO NOTHING
                RETURNING id
                """
            ),
            {
                "id": new_id,
                "kind": kind,
                "queue_key": queue_key,
                "payload": json.dumps(payload),
                "subject_table": subject_table,
                "subject_id": subject_id,
                "org_id": org_id,
            },
        )
    ).all()
    if not rows:
        return None
    return {"id": str(rows[0][0]), "inserted": True}


async def enqueue_tag_project_worker_job(
    session,
    *,
    scope: str,
    target_id: str,
    task_id: str | None,
    org_id: str | None,
    mode: str = "direct",
    after_task_id: str | None = None,
) -> dict | None:
    """Insert a ``worker_jobs(kind=TAG_PROJECT)`` row, coalescing against
    any in-flight TAG_PROJECT job for the same subject.

    ``mode`` distinguishes:
      * ``direct`` — recompute the named task or version only.
      * ``task_all_versions`` — recompute every version's effective array
        for ``task_id`` (used when a TASK assignment changes).
      * ``experiment_living_fanout`` — chunked walk over the experiment's
        ``task_experiments`` membership. ``after_task_id`` carries the
        cursor between continuation jobs.

    Returns the inserted row dict on success, or ``None`` if the insert
    was coalesced onto an existing in-flight row.
    """
    payload = {
        "scope": scope,
        "target_id": target_id,
        "task_id": task_id,
        "mode": mode,
    }
    if after_task_id is not None:
        payload["after_task_id"] = after_task_id
    subject_table, subject_id = _subject_for(scope, target_id, task_id)
    return await _insert_tag_project_with_coalescing(
        session,
        kind="TAG_PROJECT",
        queue_key=TAG_PROJECT_QUEUE_KEY,
        payload=payload,
        subject_table=subject_table,
        subject_id=subject_id,
        org_id=org_id,
    )


def _subject_for(
    scope: str, target_id: str, task_id: str | None
) -> tuple[str, str | None]:
    if scope == "VERSION":
        return "task_versions", target_id
    if scope == "TASK":
        return "tasks", target_id
    if scope == "EXPERIMENT":
        return "experiments", target_id
    return "tasks", task_id

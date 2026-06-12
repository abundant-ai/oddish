"""Worker handler that recomputes effective_tag_ids from truth.

Three modes, chosen by the enqueue site:
  * ``direct``: recompute the named task (and its current version arrays).
  * ``task_all_versions``: recompute the task projection + every version.
  * ``experiment_living_fanout``: recompute the projection for every task
    currently linked to the experiment.

For ``experiment_living_fanout`` we process at most ``_FANOUT_BATCH_SIZE``
member tasks per job and persist a cursor (``after_task_id`` = the last
processed member id). When the batch is full we enqueue a continuation
``TAG_PROJECT`` job that resumes after the cursor, so one large
experiment can't starve other work. The handler heartbeats per batch via
``_heartbeat_progress`` so the dispatcher knows the worker is alive even
when iterating thousands of members.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from oddish.core.tags_enqueue import enqueue_tag_project_worker_job
from oddish.core.tags_projection import (
    recompute_all_versions_for_task,
    recompute_task_browse_projection,
)
from oddish.db import get_session


# Bounded so one TAG_PROJECT job fits comfortably within the dispatcher's
# per-job budget even when every member task has a long version history.
_FANOUT_BATCH_SIZE = 50


async def _list_experiment_member_task_ids(
    session,
    experiment_id: str,
    *,
    after_task_id: str | None = None,
    limit: int,
) -> list[str]:
    """Return up to ``limit`` member ``task_id``s, ordered by id, strictly
    greater than ``after_task_id`` if a cursor is provided. Ordering by
    task_id makes the cursor stable across continuation jobs.
    """
    if after_task_id is None:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT task_id
                    FROM task_experiments
                    WHERE experiment_id = :exp_id
                      AND deleted_at IS NULL
                    ORDER BY task_id
                    LIMIT :limit
                    """
                ),
                {"exp_id": experiment_id, "limit": limit},
            )
        ).all()
    else:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT task_id
                    FROM task_experiments
                    WHERE experiment_id = :exp_id
                      AND deleted_at IS NULL
                      AND task_id > :after
                    ORDER BY task_id
                    LIMIT :limit
                    """
                ),
                {"exp_id": experiment_id, "after": after_task_id, "limit": limit},
            )
        ).all()
    return [str(r[0]) for r in rows]


async def _heartbeat_progress(
    session,
    *,
    target_id: str,
    batch_size: int,
    progress: dict[str, Any],
) -> None:
    """Bump the in-flight ``worker_jobs`` row's ``last_heartbeat_at`` and
    merge a small JSON progress blob into ``payload`` for observability.

    We key by (kind='TAG_PROJECT', subject_id=target_id) — the partial
    unique coalescing index guarantees at most one in-flight
    row per target.
    """
    await session.execute(
        text(
            """
            UPDATE worker_jobs
            SET last_heartbeat_at = NOW(),
                payload = COALESCE(payload, '{}'::jsonb)
                          || CAST(:progress AS JSONB)
            WHERE kind = 'TAG_PROJECT'
              AND subject_id = :target_id
              AND status IN ('RUNNING', 'RETRYING')
            """
        ),
        {
            "target_id": target_id,
            "progress": json.dumps(
                {"tag_project_progress": progress, "last_batch_size": batch_size}
            ),
        },
    )


async def run_tag_project_job(*, payload: dict[str, Any]) -> dict[str, Any]:
    scope = str(payload.get("scope") or "")
    target_id = str(payload.get("target_id") or "")
    task_id = payload.get("task_id")
    org_id = payload.get("org_id")
    mode = str(payload.get("mode") or "direct")
    after_task_id = payload.get("after_task_id")  # cursor for fan-out

    if not scope or not target_id:
        return {"skipped": True, "reason": "missing scope/target_id"}

    summary: dict[str, Any] = {
        "scope": scope,
        "target_id": target_id,
        "mode": mode,
        "tasks_recomputed": 0,
        "versions_recomputed": 0,
        "continuation_enqueued": False,
        "next_cursor": None,
    }

    async with get_session() as session:
        if scope == "VERSION":
            assert task_id is not None
            from oddish.core.tags_projection import (
                recompute_version_effective_tags,
            )

            await recompute_version_effective_tags(
                session, task_id=str(task_id), version_id=target_id
            )
            await recompute_task_browse_projection(
                session, task_id=str(task_id)
            )
            summary["task_id"] = str(task_id)
            summary["tasks_recomputed"] = 1
            summary["versions_recomputed"] = 1
            await session.commit()
            return summary

        if scope == "TASK" or mode == "task_all_versions":
            tid = str(target_id if scope == "TASK" else task_id)
            await recompute_task_browse_projection(session, task_id=tid)
            versions = await recompute_all_versions_for_task(session, task_id=tid)
            summary["task_id"] = tid
            summary["tasks_recomputed"] = 1
            summary["versions_recomputed"] = versions
            await session.commit()
            return summary

        if scope == "EXPERIMENT" and mode == "experiment_living_fanout":
            batch = await _list_experiment_member_task_ids(
                session,
                target_id,
                after_task_id=after_task_id,
                limit=_FANOUT_BATCH_SIZE,
            )
            last_processed: str | None = None
            for tid in batch:
                await recompute_task_browse_projection(session, task_id=tid)
                versions = await recompute_all_versions_for_task(
                    session, task_id=tid
                )
                summary["tasks_recomputed"] += 1
                summary["versions_recomputed"] += versions
                last_processed = tid

            await _heartbeat_progress(
                session,
                target_id=target_id,
                batch_size=len(batch),
                progress={
                    "after_task_id": after_task_id,
                    "last_processed": last_processed,
                    "in_batch": len(batch),
                },
            )

            if len(batch) == _FANOUT_BATCH_SIZE and last_processed is not None:
                await enqueue_tag_project_worker_job(
                    session,
                    scope="EXPERIMENT",
                    target_id=target_id,
                    task_id=None,
                    org_id=org_id,
                    mode="experiment_living_fanout",
                    after_task_id=last_processed,
                )
                summary["continuation_enqueued"] = True
                summary["next_cursor"] = last_processed
            await session.commit()
            return summary

        return summary

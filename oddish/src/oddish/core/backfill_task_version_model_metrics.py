"""Populate task_version_model_metrics for task versions that have no row yet.

Batched and resumable: each batch commits on its own and the next run picks up
where this one stopped, because progress is the presence of rows rather than a
stored cursor. Safe to re-run -- a recomputed group produces an identical row.

    uv run python -m oddish.core.backfill_task_version_model_metrics --limit 500
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from sqlalchemy import func, select

from oddish.core.task_version_model_metrics import (
    metrics_query,
    refresh_task_version_model_metrics,
)
from oddish.db import TaskVersionModel, TaskVersionModelMetricsModel, get_session

logger = logging.getLogger(__name__)

DEFAULT_BATCH = 200


async def _next_version_ids(session, after_id: str | None, batch: int) -> list[str]:
    """Next page of task versions in id order.

    Keyset, not "versions missing a row": a version whose trials are all out of
    scope legitimately produces no rows, so a missing-row cursor would hand back
    the same page forever.
    """
    query = select(TaskVersionModel.id).order_by(TaskVersionModel.id.asc())
    if after_id is not None:
        query = query.where(TaskVersionModel.id > after_id)
    rows = await session.execute(query.limit(batch))
    return [str(value) for value in rows.scalars().all()]


async def backfill(
    batch: int = DEFAULT_BATCH,
    limit: int | None = None,
    after_id: str | None = None,
) -> int:
    """Recompute metrics for every task version. Returns the number processed."""
    processed = 0
    cursor = after_id
    while limit is None or processed < limit:
        size = batch if limit is None else min(batch, limit - processed)
        async with get_session() as session:
            version_ids = await _next_version_ids(session, cursor, size)
            if not version_ids:
                break
            await refresh_task_version_model_metrics(session, version_ids)
            written = (
                await session.execute(
                    select(func.count())
                    .select_from(TaskVersionModelMetricsModel)
                    .where(
                        TaskVersionModelMetricsModel.task_version_id.in_(version_ids)
                    )
                )
            ).scalar_one()
            await session.commit()
        cursor = version_ids[-1]
        processed += len(version_ids)
        logger.info(
            "%d task versions (%d metric rows); %d total; resume after %s",
            len(version_ids),
            written,
            processed,
            cursor,
        )
        if len(version_ids) < size:
            break
    return processed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    parser.add_argument(
        "--limit", type=int, default=None, help="stop after N task versions"
    )
    parser.add_argument(
        "--after-id", default=None, help="resume after this task version id"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    total = asyncio.run(
        backfill(batch=args.batch, limit=args.limit, after_id=args.after_id)
    )
    logger.info("done: %d task versions", total)


if __name__ == "__main__":
    main()


__all__ = ["backfill", "metrics_query"]

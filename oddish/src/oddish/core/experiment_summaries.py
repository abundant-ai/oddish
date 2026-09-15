"""Bounded, restart-safe maintenance of prepared dashboard rows."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import select, text, update

from oddish.db import ExperimentModel, get_session, utcnow
from oddish.db.models import ExperimentSummaryModel

logger = logging.getLogger(__name__)


async def refresh_experiment_summaries(*, batch_size: int = 32) -> int:
    """One database-wide maintainer; writers never wait for a rebuild lock.

    The advisory lock serializes maintainers without locking dirty-marker rows.
    A concurrent mutation increments revision; publication only acknowledges the
    captured revision, leaving the newer work pending. A killed worker rolls back
    its publication and the next scheduled invocation retries. Daily rotation
    recomputes even clean summaries, repairing missed dependencies automatically.
    """
    from oddish.core.dashboard import (
        dashboard_experiment_rows,
        rebuild_dashboard_experiments,
    )

    completed = 0
    publications = []
    retries = []
    started = time.monotonic()
    async with get_session() as session:
        if not await session.scalar(
            text("SELECT pg_try_advisory_xact_lock(716490241)")
        ):
            return 0
        await session.execute(text("SET LOCAL statement_timeout = '20s'"))
        now = utcnow()
        summary = ExperimentSummaryModel
        candidates = (
            await session.execute(
                select(
                    summary.experiment_id,
                    summary.revision,
                    summary.dirty_since,
                    ExperimentModel.org_id,
                    (summary.revision > summary.built_revision).label("pending"),
                )
                .join(ExperimentModel, ExperimentModel.id == summary.experiment_id)
                .where(
                    summary.next_attempt_at <= now,
                    (summary.revision > summary.built_revision)
                    | (summary.refreshed_at < now - timedelta(days=1)),
                )
                .order_by(
                    (summary.revision > summary.built_revision).desc(),
                    summary.next_attempt_at,
                    summary.dirty_since,
                )
                .limit(batch_size)
            )
        ).all()
        groups = defaultdict(list)
        for experiment_id, revision, dirty_since, org_id, pending in candidates:
            groups[org_id].append((experiment_id, revision, dirty_since, pending))
        for org_id, group in groups.items():
            if time.monotonic() - started > 25:
                break
            ids = [row[0] for row in group]
            revisions = {row[0]: row[1] for row in group}
            try:
                async with get_session() as reader, asyncio.timeout(20):
                    await reader.execute(text("SET LOCAL statement_timeout = '15s'"))
                    rows = (
                        (
                            await reader.execute(
                                dashboard_experiment_rows().where(
                                    ExperimentModel.id.in_(ids),
                                    ExperimentModel.org_id.is_(None)
                                    if org_id is None
                                    else ExperimentModel.org_id == org_id,
                                )
                            )
                        )
                        .mappings()
                        .all()
                    )
                    payloads = (
                        await rebuild_dashboard_experiments(
                            reader, page_rows=rows, org_id=org_id
                        )
                        if rows
                        else []
                    )
                    publications.extend(
                        (payload["id"], revisions[payload["id"]], payload)
                        for payload in payloads
                    )
                    pending_times = [row[2] for row in group if row[3]]
                    oldest = min(pending_times) if pending_times else now
                    if (now - oldest).total_seconds() > 60:
                        logger.warning(
                            "dashboard summary lag org_id=%s lag_seconds=%.1f",
                            org_id,
                            (now - oldest).total_seconds(),
                        )
            except Exception:
                logger.exception(
                    "dashboard summary rebuild failed org_id=%s experiment_ids=%s",
                    org_id,
                    ids,
                )
                retries.extend(ids)
        # Lock only after calculation. Never wait on a writer while holding
        # another summary lock: task writes may touch several experiments.
        # Skipped rows retain their revision and retry time for the next pass.
        ready_ids = {row[0] for row in publications} | set(retries)
        locked_ids = (
            set(
                await session.scalars(
                    select(summary.experiment_id)
                    .where(summary.experiment_id.in_(ready_ids))
                    .with_for_update(skip_locked=True)
                )
            )
            if ready_ids
            else set()
        )
        for experiment_id, revision, payload in publications:
            if experiment_id not in locked_ids:
                continue
            await session.execute(
                update(summary)
                .where(summary.experiment_id == experiment_id)
                .values(
                    payload=payload,
                    built_revision=revision,
                    refreshed_at=now,
                    next_attempt_at=now,
                )
            )
            completed += 1
        for experiment_id in retries:
            if experiment_id not in locked_ids:
                continue
            await session.execute(
                update(summary)
                .where(summary.experiment_id == experiment_id)
                .values(next_attempt_at=now + timedelta(seconds=30))
            )
        await session.commit()
    return completed


async def run_experiment_summary_maintenance() -> None:
    """Standalone worker lifecycle; hosted deployment schedules the same batch."""
    while True:
        try:
            await refresh_experiment_summaries()
        except Exception:
            logger.exception("dashboard summary maintenance failed")
        await asyncio.sleep(2)


async def prepared_read_health() -> dict[str, int | float]:
    """Independent scheduled health sampling also detects a stopped maintainer."""
    from sqlalchemy import func
    from oddish.db import get_read_session
    from oddish.db.models import FileIndexModel

    async with get_read_session() as session:
        summary_count, oldest = (
            await session.execute(
                select(func.count(), func.min(ExperimentSummaryModel.dirty_since))
                .select_from(ExperimentSummaryModel)
                .join(
                    ExperimentModel,
                    ExperimentModel.id == ExperimentSummaryModel.experiment_id,
                )
                .where(
                    ExperimentSummaryModel.revision
                    > ExperimentSummaryModel.built_revision
                )
            )
        ).one()
        files = await session.scalar(
            select(func.count())
            .select_from(FileIndexModel)
            .where(FileIndexModel.revision.is_(None))
        )
    return {
        "summary_pending": summary_count,
        "summary_lag_seconds": max(0.0, (utcnow() - oldest).total_seconds())
        if oldest
        else 0.0,
        "file_index_pending": files or 0,
    }

"""Hourly delivery observations. Run with python -m oddish.core.delivery_progress."""

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db import (
    DeliveryModel,
    DeliveryProgressModel,
    get_read_session,
    get_session,
    utcnow,
)
from oddish.schemas import DeliveryBoardResponse, DeliveryProgressPoint


async def record_delivery_progress(
    session: AsyncSession,
    board: DeliveryBoardResponse,
    *,
    recorded_at: datetime | None = None,
) -> None:
    now = recorded_at or utcnow()
    blocked = sum(
        (
            any(c.kind == "automated" and c.status == "fail" for c in row.checks)
            or any(not d.acknowledged for d in row.defects)
        )
        for row in board.tasks
    )
    point = DeliveryProgressPoint(
        recorded_at=now,
        task_count=board.task_count,
        ready=board.ready_task_count,
        blocked=blocked,
        awaiting_signoff=board.task_count - board.ready_task_count - blocked,
        unassigned=sum(
            not row.ready and not row.qa_work.owner_user_id for row in board.tasks
        ),
        open_findings=sum(
            not d.acknowledged for row in board.tasks for d in row.defects
        ),
        acknowledged_findings=sum(
            d.acknowledged for row in board.tasks for d in row.defects
        ),
    )
    stmt = insert(DeliveryProgressModel).values(
        delivery_id=board.delivery.id,
        sample_hour=now.replace(minute=0, second=0, microsecond=0),
        recorded_at=now,
        counts=point.model_dump(exclude={"recorded_at"}),
    )
    # Retry-safe; finalization replaces the current hour with the actual shipped counts.
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["delivery_id", "sample_hour"],
            set_={
                "recorded_at": stmt.excluded.recorded_at,
                "counts": stmt.excluded.counts,
            },
        )
    )


async def delivery_progress_history(
    session: AsyncSession, delivery_id: str
) -> list[DeliveryProgressPoint]:
    # Latest observation per UTC day, including today. A missed day stays absent.
    day = func.date_trunc(
        "day", DeliveryProgressModel.recorded_at.op("AT TIME ZONE")("UTC")
    )
    rows = await session.scalars(
        select(DeliveryProgressModel)
        .where(
            DeliveryProgressModel.delivery_id == delivery_id,
            DeliveryProgressModel.sample_hour
            >= utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            - timedelta(days=29),
        )
        .distinct(day)
        .order_by(day.desc(), DeliveryProgressModel.recorded_at.desc())
        .limit(30)
    )
    return list(
        reversed(
            [
                DeliveryProgressPoint(recorded_at=row.recorded_at, **row.counts)
                for row in rows
            ]
        )
    )


async def sample_active_deliveries() -> None:
    from oddish.core.deliveries import _compute_board, _get_delivery

    async with get_read_session() as session:
        deliveries = (
            await session.execute(
                select(DeliveryModel.id, DeliveryModel.org_id).where(
                    DeliveryModel.status == "active"
                )
            )
        ).all()
    for delivery_id, org_id in deliveries:
        try:
            # Commit each delivery separately; a failed sample must not erase other observations.
            async with get_session() as session:
                delivery = await _get_delivery(
                    session, delivery_id, org_id, for_update=True
                )
                if delivery.status == "active":
                    await record_delivery_progress(
                        session, await _compute_board(session, delivery)
                    )
                    await session.commit()
        except Exception:
            logging.exception("Delivery progress sample failed: %s", delivery_id)


if __name__ == "__main__":
    asyncio.run(sample_active_deliveries())

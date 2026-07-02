from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.quotas import (  # noqa: E402
    start_of_today_utc,
    sum_cost_usd,
    sum_cost_usd_by_user,
)
from oddish.db import (  # noqa: E402
    TaskModel,
    TrialModel,
    WorkerJobModel,
    get_session,
)
from oddish.queue import create_task  # noqa: E402
from oddish.schemas import TaskSubmission, TrialSpec  # noqa: E402

_RUN = uuid.uuid4().hex[:8]


def _submission(name: str, *, n_trials: int) -> TaskSubmission:
    return TaskSubmission(
        name=name,
        task_path="s3://test-bucket/quota-usage-fake-task",
        trials=[TrialSpec(agent="nop", model=None) for _ in range(n_trials)],
    )


@pytest_asyncio.fixture
async def cleanup_task_ids():
    task_ids: list[str] = []
    yield task_ids
    async with get_session() as session:
        for task_id in task_ids:
            await session.execute(
                WorkerJobModel.__table__.delete().where(
                    WorkerJobModel.subject_id == task_id
                )
            )
            await session.execute(
                TaskModel.__table__.delete().where(TaskModel.id == task_id)
            )


def test_start_of_today_utc_is_calendar_midnight_boundary():
    last_moment_of_the_day = datetime(2026, 6, 30, 23, 59, 59, 999000, tzinfo=timezone.utc)
    first_moment_of_the_day = datetime(2026, 6, 30, 0, 0, 0, tzinfo=timezone.utc)
    first_moment_of_next_day = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc)

    assert start_of_today_utc(last_moment_of_the_day) == first_moment_of_the_day
    assert start_of_today_utc(first_moment_of_the_day) == first_moment_of_the_day
    assert start_of_today_utc(last_moment_of_the_day).tzinfo is not None
    assert start_of_today_utc(first_moment_of_next_day) > start_of_today_utc(
        last_moment_of_the_day
    )


@pytest.mark.asyncio
async def test_sum_cost_usd_excludes_inflight_before_window_and_null_billed(
    cleanup_task_ids,
):
    task_id = f"quota-sum-{_RUN}"
    cleanup_task_ids.append(task_id)
    org_id = f"org-quota-sum-{_RUN}"
    billed_user = f"user-A-{_RUN}"

    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)

    async with get_session() as session:
        await create_task(
            session,
            _submission("quota-sum", n_trials=6),
            task_id=task_id,
            org_id=org_id,
            billed_user_id=billed_user,
        )
        await session.flush()

        counted_today_one = await session.get(TrialModel, f"{task_id}-0")
        counted_today_one.finished_at = now
        counted_today_one.cost_usd = 0.10

        counted_today_two = await session.get(TrialModel, f"{task_id}-1")
        counted_today_two.finished_at = now
        counted_today_two.cost_usd = 0.20

        still_in_flight = await session.get(TrialModel, f"{task_id}-2")
        still_in_flight.finished_at = None
        still_in_flight.cost_usd = 5.00

        # Soft-deleted settled spend still counts: deleting is not a budget reset.
        soft_deleted_but_counted = await session.get(TrialModel, f"{task_id}-3")
        soft_deleted_but_counted.finished_at = now
        soft_deleted_but_counted.cost_usd = 9.00
        soft_deleted_but_counted.deleted_at = now

        billed_to_nobody = await session.get(TrialModel, f"{task_id}-4")
        billed_to_nobody.finished_at = now
        billed_to_nobody.cost_usd = 7.00
        billed_to_nobody.billed_user_id = None

        finished_before_today = await session.get(TrialModel, f"{task_id}-5")
        finished_before_today.finished_at = yesterday
        finished_before_today.cost_usd = 3.00

        await session.flush()

        settled_today = await sum_cost_usd(
            session, org_id, billed_user, start_of_today_utc(now)
        )
        grouped = await sum_cost_usd_by_user(session, org_id, start_of_today_utc(now))

    assert settled_today == Decimal("9.30")
    assert grouped[billed_user] == Decimal("9.30")

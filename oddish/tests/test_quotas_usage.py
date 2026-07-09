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
    quota_window_start,
    sum_cost_usd,
    sum_cost_usd_by_user,
    to_money_decimal,
)
from oddish.model_pricing import estimate_cost_usd  # noqa: E402
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


def test_quota_window_start_is_a_rolling_24h_boundary():
    now = datetime(2026, 6, 30, 15, 30, 45, 123000, tzinfo=timezone.utc)

    window_start = quota_window_start(now)
    assert window_start == datetime(
        2026, 6, 29, 15, 30, 45, 123000, tzinfo=timezone.utc
    )
    assert window_start.tzinfo is not None
    assert quota_window_start(now + timedelta(seconds=1)) == window_start + timedelta(
        seconds=1
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
    aged_out = now - timedelta(hours=24)

    async with get_session() as session:
        await create_task(
            session,
            _submission("quota-sum", n_trials=6),
            task_id=task_id,
            org_id=org_id,
            billed_user_id=billed_user,
        )
        await session.flush()

        counted_in_window_one = await session.get(TrialModel, f"{task_id}-0")
        counted_in_window_one.finished_at = now
        counted_in_window_one.cost_usd = 0.10

        counted_in_window_two = await session.get(TrialModel, f"{task_id}-1")
        counted_in_window_two.finished_at = now
        counted_in_window_two.cost_usd = 0.20

        still_in_flight = await session.get(TrialModel, f"{task_id}-2")
        still_in_flight.finished_at = None
        still_in_flight.cost_usd = 5.00

        soft_deleted_but_counted = await session.get(TrialModel, f"{task_id}-3")
        soft_deleted_but_counted.finished_at = now
        soft_deleted_but_counted.cost_usd = 9.00
        soft_deleted_but_counted.deleted_at = now

        billed_to_nobody = await session.get(TrialModel, f"{task_id}-4")
        billed_to_nobody.finished_at = now
        billed_to_nobody.cost_usd = 7.00
        billed_to_nobody.billed_user_id = None

        finished_before_window = await session.get(TrialModel, f"{task_id}-5")
        finished_before_window.finished_at = aged_out
        finished_before_window.cost_usd = 3.00

        await session.flush()

        settled_in_window = await sum_cost_usd(
            session, org_id, billed_user, quota_window_start(now)
        )
        grouped = await sum_cost_usd_by_user(session, org_id, quota_window_start(now))

    assert settled_in_window == Decimal("9.30")
    assert grouped[billed_user] == Decimal("9.30")


@pytest.mark.asyncio
async def test_unpriced_finished_trial_is_token_estimated(cleanup_task_ids):
    """Unpriced finished trials are token-estimated (not floored); a cancelled
    unpriced trial and an unpriced trial with no tokens both count $0."""
    task_id = f"quota-unpriced-{_RUN}"
    cleanup_task_ids.append(task_id)
    org_id = f"org-quota-unpriced-{_RUN}"
    billed_user = f"user-unpriced-{_RUN}"
    now = datetime.now(timezone.utc)
    # A model with deterministic local pricing so the estimate is stable.
    model = "gpt-5.5-pro"
    est = estimate_cost_usd(model, 1_000_000, 500_000, 0)
    assert est and est > 0  # guard the fixture: pricing must resolve

    async with get_session() as session:
        await create_task(
            session,
            _submission("quota-unpriced", n_trials=5),
            task_id=task_id,
            org_id=org_id,
            billed_user_id=billed_user,
        )
        await session.flush()

        # Unpriced but with tokens -> token estimate.
        estimated = await session.get(TrialModel, f"{task_id}-0")
        estimated.started_at = now
        estimated.finished_at = now
        estimated.cost_usd = None
        estimated.model = model
        estimated.input_tokens = 1_000_000
        estimated.output_tokens = 500_000
        estimated.cache_tokens = 0

        # Priced -> native cost.
        priced = await session.get(TrialModel, f"{task_id}-1")
        priced.finished_at = now
        priced.cost_usd = 0.25

        # Free (cost 0) -> 0.
        free = await session.get(TrialModel, f"{task_id}-2")
        free.finished_at = now
        free.cost_usd = 0.0

        # Unpriced with NO tokens -> $0 (the floor defaults to $0).
        no_tokens = await session.get(TrialModel, f"{task_id}-3")
        no_tokens.started_at = now
        no_tokens.finished_at = now
        no_tokens.cost_usd = None

        # Cancelled (harbor_stage='cancelled') unpriced WITH tokens -> $0.
        cancelled = await session.get(TrialModel, f"{task_id}-4")
        cancelled.started_at = now
        cancelled.finished_at = now
        cancelled.cost_usd = None
        cancelled.model = model
        cancelled.input_tokens = 1_000_000
        cancelled.output_tokens = 500_000
        cancelled.harbor_stage = "cancelled"

        await session.flush()

        settled = await sum_cost_usd(
            session, org_id, billed_user, quota_window_start(now)
        )
        grouped = await sum_cost_usd_by_user(session, org_id, quota_window_start(now))

    expected = to_money_decimal(est + 0.25)
    assert settled == expected
    assert grouped[billed_user] == expected

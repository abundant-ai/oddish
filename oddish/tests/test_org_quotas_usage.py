from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import settings  # noqa: E402
from oddish.core.quotas import (  # noqa: E402
    get_effective_org_limit,
    org_inflight_reserved_usd,
    start_of_today_utc,
    sum_org_cost_usd,
)
from oddish.db import (  # noqa: E402
    TaskModel,
    TrialModel,
    TrialStatus,
    WorkerJobModel,
    get_session,
)
from oddish.queue import create_task  # noqa: E402
from oddish.schemas import TaskSubmission, TrialSpec  # noqa: E402

_RUN = uuid.uuid4().hex[:8]


def _submission(name: str, *, n_trials: int) -> TaskSubmission:
    return TaskSubmission(
        name=name,
        task_path="s3://test-bucket/org-quota-usage-fake-task",
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
                    WorkerJobModel.subject_id.like(f"{task_id}%")
                )
            )
            await session.execute(
                TaskModel.__table__.delete().where(TaskModel.id == task_id)
            )


# --- sum_org_cost_usd: whole-org total, NULL-billed + soft-deleted count -------


@pytest.mark.asyncio
async def test_sum_org_cost_usd_counts_all_payers_null_billed_and_soft_deleted(
    cleanup_task_ids,
):
    task_id = f"org-sum-{_RUN}"
    cleanup_task_ids.append(task_id)
    org_id = f"org-orgsum-{_RUN}"
    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)
    floor = settings.unpriced_trial_cost_usd

    async with get_session() as session:
        await create_task(
            session,
            _submission("org-sum", n_trials=7),
            task_id=task_id,
            org_id=org_id,
            billed_user_id=f"user-A-{_RUN}",
        )
        await session.flush()

        # Two different payers both count toward the org total.
        payer_a = await session.get(TrialModel, f"{task_id}-0")
        payer_a.finished_at = now
        payer_a.cost_usd = 0.10

        payer_b = await session.get(TrialModel, f"{task_id}-1")
        payer_b.finished_at = now
        payer_b.cost_usd = 0.20
        payer_b.billed_user_id = f"user-B-{_RUN}"

        # Unattributed (NULL-billed) settled spend DOES count for the org.
        null_billed = await session.get(TrialModel, f"{task_id}-2")
        null_billed.finished_at = now
        null_billed.cost_usd = 7.00
        null_billed.billed_user_id = None

        # Soft-deleted settled spend still counts (delete is not a reset).
        soft_deleted = await session.get(TrialModel, f"{task_id}-3")
        soft_deleted.finished_at = now
        soft_deleted.cost_usd = 9.00
        soft_deleted.deleted_at = now

        # Unpriced (started, cost NULL) floors at unpriced_trial_cost_usd.
        unpriced = await session.get(TrialModel, f"{task_id}-4")
        unpriced.started_at = now
        unpriced.finished_at = now
        unpriced.cost_usd = None

        # Still in-flight -> excluded from the settled SUM.
        in_flight = await session.get(TrialModel, f"{task_id}-5")
        in_flight.finished_at = None
        in_flight.cost_usd = 5.00

        # Finished before today -> outside the window.
        before_today = await session.get(TrialModel, f"{task_id}-6")
        before_today.finished_at = yesterday
        before_today.cost_usd = 3.00

        await session.flush()

        org_total = await sum_org_cost_usd(session, org_id, start_of_today_utc(now))

    assert org_total == Decimal("0.10") + Decimal("0.20") + Decimal("7.00") + Decimal(
        "9.00"
    ) + floor


# --- org_inflight_reserved_usd: org-wide in-flight, all payers ------------------


@pytest.mark.asyncio
async def test_org_inflight_reserved_usd_sums_all_payers(cleanup_task_ids, monkeypatch):
    monkeypatch.setattr(settings, "pending_trial_reservation_usd", Decimal("0.20"))
    task_id = f"org-inflight-{_RUN}"
    cleanup_task_ids.append(task_id)
    org_id = f"org-orginflight-{_RUN}"

    async with get_session() as session:
        await create_task(
            session,
            _submission("org-inflight", n_trials=3),
            task_id=task_id,
            org_id=org_id,
            billed_user_id=f"user-A-{_RUN}",
        )
        await session.flush()

        # A QUEUED trial (no cost yet) -> floored at the pending reservation.
        queued = await session.get(TrialModel, f"{task_id}-0")
        queued.status = TrialStatus.QUEUED

        # A RETRYING trial billed to a different payer with accumulated cost.
        retrying = await session.get(TrialModel, f"{task_id}-1")
        retrying.status = TrialStatus.RETRYING
        retrying.cost_usd = 3.00
        retrying.billed_user_id = f"user-B-{_RUN}"

        # Finished -> excluded from the in-flight reservation.
        finished = await session.get(TrialModel, f"{task_id}-2")
        finished.finished_at = datetime.now(timezone.utc)
        finished.cost_usd = 1.00

        await session.flush()

        reserved = await org_inflight_reserved_usd(session, org_id)

    # max(0, 0.20) + max(3.00, 0.20) = 0.20 + 3.00
    assert reserved == Decimal("3.20")


# --- get_effective_org_limit: override > default > None -------------------------


@pytest.mark.asyncio
async def test_get_effective_org_limit_precedence(monkeypatch):
    org_id = f"org-eff-{_RUN}-{uuid.uuid4().hex[:6]}"

    # No override, no default -> None (no org cap).
    monkeypatch.setattr(settings, "default_org_daily_quota_usd", None)
    async with get_session() as session:
        assert await get_effective_org_limit(session, org_id) is None

    # No override, default configured -> the default.
    monkeypatch.setattr(settings, "default_org_daily_quota_usd", Decimal("50.00"))
    async with get_session() as session:
        assert await get_effective_org_limit(session, org_id) == Decimal("50.00")

    # A live override row wins over the default.
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO organizations "
                "(id, name, slug, plan, settings, is_active, created_at, updated_at) "
                "VALUES (:id, :id, :id, 'free', '{}'::jsonb, true, NOW(), NOW())"
            ),
            {"id": org_id},
        )
        await session.execute(
            text(
                "INSERT INTO org_quotas "
                "(id, org_id, limit_usd, period_kind, created_at, updated_at) "
                "VALUES (:qid, :org_id, :limit, 'daily', NOW(), NOW())"
            ),
            {"qid": uuid.uuid4().hex[:8], "org_id": org_id, "limit": Decimal("7.00")},
        )
        await session.flush()
    try:
        async with get_session() as session:
            assert await get_effective_org_limit(session, org_id) == Decimal("7.00")

        # A soft-deleted override does not enforce -> falls back to the default.
        async with get_session() as session:
            await session.execute(
                text("UPDATE org_quotas SET deleted_at = NOW() WHERE org_id = :o"),
                {"o": org_id},
            )
        async with get_session() as session:
            assert await get_effective_org_limit(session, org_id) == Decimal("50.00")
    finally:
        async with get_session() as session:
            await session.execute(
                text("DELETE FROM org_quotas WHERE org_id = :o"), {"o": org_id}
            )
            await session.execute(
                text("DELETE FROM organizations WHERE id = :o"), {"o": org_id}
            )

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
    start_of_month_utc,
    start_of_today_utc,
    sum_org_cost_usd,
)
from oddish.db import (  # noqa: E402
    TaskModel,
    TrialModel,
    TrialOrigin,
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


_ORG_IDS: set[str] = set()


async def _ensure_org(org_id: str) -> None:
    """Seed the organizations row that tasks.org_id FKs to (the combined
    oddish+backend dev DB enforces that FK)."""
    _ORG_IDS.add(org_id)
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO organizations "
                "(id, name, slug, plan, settings, is_active, created_at, updated_at) "
                "VALUES (:id, :id, :id, 'free', '{}'::jsonb, true, NOW(), NOW()) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": org_id},
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
        for org_id in list(_ORG_IDS):
            await session.execute(
                text("DELETE FROM org_quotas WHERE org_id = :o"), {"o": org_id}
            )
            await session.execute(
                text("DELETE FROM organizations WHERE id = :o"), {"o": org_id}
            )
    _ORG_IDS.clear()


# --- start_of_month_utc: calendar UTC month floor ------------------------------


def test_start_of_month_utc_midmonth():
    now = datetime(2026, 7, 6, 17, 29, 30, tzinfo=timezone.utc)
    assert start_of_month_utc(now) == datetime(2026, 7, 1, tzinfo=timezone.utc)


def test_start_of_month_utc_first_of_month_instant():
    now = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert start_of_month_utc(now) == datetime(2026, 7, 1, tzinfo=timezone.utc)


def test_start_of_month_utc_year_boundary():
    now = datetime(2026, 1, 1, 3, 15, 0, tzinfo=timezone.utc)
    assert start_of_month_utc(now) == datetime(2026, 1, 1, tzinfo=timezone.utc)
    # Late December stays in December, not the next year.
    dec = datetime(2025, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    assert start_of_month_utc(dec) == datetime(2025, 12, 1, tzinfo=timezone.utc)


# --- sum_org_cost_usd: whole-org total, NULL-billed + soft-deleted count -------


@pytest.mark.asyncio
async def test_sum_org_cost_usd_counts_all_payers_null_billed_and_soft_deleted(
    cleanup_task_ids,
):
    task_id = f"org-sum-{_RUN}"
    cleanup_task_ids.append(task_id)
    org_id = f"org-orgsum-{_RUN}"
    now = datetime.now(timezone.utc)
    before_month = start_of_month_utc(now) - timedelta(seconds=1)

    await _ensure_org(org_id)
    async with get_session() as session:
        await create_task(
            session,
            _submission("org-sum", n_trials=9),
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

        # Unpriced (started, cost NULL) with no tokens -> $0 (floor defaults $0).
        unpriced = await session.get(TrialModel, f"{task_id}-4")
        unpriced.started_at = now
        unpriced.finished_at = now
        unpriced.cost_usd = None

        # Still in-flight -> excluded from the settled SUM.
        in_flight = await session.get(TrialModel, f"{task_id}-5")
        in_flight.finished_at = None
        in_flight.cost_usd = 5.00

        # Finished before this month -> outside the monthly window.
        last_month = await session.get(TrialModel, f"{task_id}-6")
        last_month.finished_at = before_month
        last_month.cost_usd = 3.00

        # Imported runs were paid for outside Oddish and do not consume quota.
        imported = await session.get(TrialModel, f"{task_id}-7")
        imported.finished_at = now
        imported.cost_usd = 10_000.00
        imported.origin = TrialOrigin.IMPORTED

        # Combine rows materialize an existing result; charging their copied
        # cost would count the same execution twice.
        combined = await session.get(TrialModel, f"{task_id}-8")
        combined.finished_at = now
        combined.cost_usd = 20_000.00
        combined.idempotency_key = f"combine:result:{task_id}-0"

        await session.flush()

        org_total = await sum_org_cost_usd(session, org_id, start_of_month_utc(now))

    assert (
        org_total
        == Decimal("0.10") + Decimal("0.20") + Decimal("7.00") + Decimal("9.00")
    )


@pytest.mark.asyncio
async def test_sum_org_cost_usd_excludes_last_month_spend(cleanup_task_ids):
    """Monthly boundary: spend finished before the 1st of the current UTC month
    is excluded, and spend at exactly the 1st 00:00:00 UTC is included (the org
    cap is a calendar-month budget that resets on the 1st; the boundary instant
    belongs to the new month, unlike the per-user rolling window's exclusive
    ``>``)."""
    task_id = f"org-month-{_RUN}"
    cleanup_task_ids.append(task_id)
    org_id = f"org-orgmonth-{_RUN}"
    now = datetime.now(timezone.utc)
    month_start = start_of_month_utc(now)

    await _ensure_org(org_id)
    async with get_session() as session:
        await create_task(
            session,
            _submission("org-month", n_trials=3),
            task_id=task_id,
            org_id=org_id,
            billed_user_id=f"user-M-{_RUN}",
        )
        await session.flush()

        # Finished last month -> excluded.
        last_month = await session.get(TrialModel, f"{task_id}-0")
        last_month.finished_at = month_start - timedelta(days=2)
        last_month.cost_usd = 40.00

        # Finished exactly at the month boundary (the 1st, 00:00:00 UTC) ->
        # INCLUDED: calendar-anchored org windows are inclusive, the instant
        # of midnight on the 1st belongs to the new month.
        on_boundary = await session.get(TrialModel, f"{task_id}-1")
        on_boundary.finished_at = month_start
        on_boundary.cost_usd = 4.00

        # Finished this month -> counted.
        this_month = await session.get(TrialModel, f"{task_id}-2")
        this_month.finished_at = now
        this_month.cost_usd = 2.50

        await session.flush()

        org_total = await sum_org_cost_usd(session, org_id, month_start)

    assert org_total == Decimal("4.00") + Decimal("2.50")


# --- org_inflight_reserved_usd: org-wide in-flight, all payers ------------------


@pytest.mark.asyncio
async def test_org_inflight_reserved_usd_sums_all_payers(cleanup_task_ids, monkeypatch):
    monkeypatch.setattr(settings, "pending_trial_reservation_usd", Decimal("0.20"))
    task_id = f"org-inflight-{_RUN}"
    cleanup_task_ids.append(task_id)
    org_id = f"org-orginflight-{_RUN}"

    await _ensure_org(org_id)
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
    monkeypatch.setattr(settings, "default_org_monthly_quota_usd", None)
    async with get_session() as session:
        assert await get_effective_org_limit(session, org_id) is None

    # No override, default configured -> the default.
    monkeypatch.setattr(settings, "default_org_monthly_quota_usd", Decimal("50.00"))
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
                "VALUES (:qid, :org_id, :limit, 'monthly', NOW(), NOW())"
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


def test_start_of_today_utc_floors_to_midnight():
    now = datetime(2026, 7, 6, 17, 29, 30, tzinfo=timezone.utc)
    assert start_of_today_utc(now) == datetime(2026, 7, 6, tzinfo=timezone.utc)

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
    effective_limits_by_org_user_all_orgs,
    quota_window_start,
    start_of_month_utc,
    sum_cost_usd,
    sum_cost_usd_by_org_user_all_orgs,
    sum_cost_usd_by_user,
    sum_org_cost_usd,
    to_money_decimal,
)
from oddish.db import (  # noqa: E402
    AnalysisCostModel,
    ModalCostSpanModel,
    TaskModel,
    TrialModel,
    WorkerJobModel,
    get_session,
)
from oddish.queue import create_task  # noqa: E402
from oddish.schemas import TaskSubmission, TrialSpec  # noqa: E402

_RUN = uuid.uuid4().hex[:8]
_ORG_IDS: set[str] = set()
_USER_IDS: set[str] = set()
_ANALYSIS_IDS: list[str] = []
_MODAL_IDS: list[str] = []


def _submission(name: str, *, n_trials: int) -> TaskSubmission:
    return TaskSubmission(
        name=name,
        task_path="s3://test-bucket/quota-composite-fake-task",
        trials=[TrialSpec(agent="nop", model=None) for _ in range(n_trials)],
    )


async def _ensure_org(org_id: str) -> None:
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


async def _ensure_user(org_id: str, user_id: str) -> None:
    await _ensure_org(org_id)
    _USER_IDS.add(user_id)
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO users "
                "(id, org_id, email, role, is_active, created_at, updated_at) "
                "VALUES (:id, :org_id, :email, 'member'::userrole, true, NOW(), NOW()) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": user_id,
                "org_id": org_id,
                "email": f"{user_id}@example.com",
            },
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
        if _ANALYSIS_IDS:
            await session.execute(
                AnalysisCostModel.__table__.delete().where(
                    AnalysisCostModel.id.in_(_ANALYSIS_IDS)
                )
            )
        if _MODAL_IDS:
            await session.execute(
                ModalCostSpanModel.__table__.delete().where(
                    ModalCostSpanModel.id.in_(_MODAL_IDS)
                )
            )
        for user_id in list(_USER_IDS):
            await session.execute(
                text("DELETE FROM quota_bumps WHERE user_id = :u"), {"u": user_id}
            )
            await session.execute(
                text("DELETE FROM quotas WHERE user_id = :u"), {"u": user_id}
            )
            await session.execute(
                text("DELETE FROM users WHERE id = :u"), {"u": user_id}
            )
        for org_id in list(_ORG_IDS):
            await session.execute(
                text("DELETE FROM quota_bumps WHERE org_id = :o"), {"o": org_id}
            )
            await session.execute(
                text("DELETE FROM quotas WHERE org_id = :o"), {"o": org_id}
            )
            await session.execute(
                text("DELETE FROM organizations WHERE id = :o"), {"o": org_id}
            )
    _ORG_IDS.clear()
    _USER_IDS.clear()
    _ANALYSIS_IDS.clear()
    _MODAL_IDS.clear()


def _analysis(
    *,
    org_id: str,
    billed_user_id: str,
    cost_usd: float,
    created_at: datetime,
    deleted_at: datetime | None = None,
) -> AnalysisCostModel:
    row = AnalysisCostModel(
        id=f"acomp-{_RUN}-{uuid.uuid4().hex[:8]}",
        job_kind="trial_classifier",
        org_id=org_id,
        billed_user_id=billed_user_id,
        model="claude-haiku-4-5",
        cost_usd=cost_usd,
        cost_source="native",
        created_at=created_at,
        deleted_at=deleted_at,
    )
    _ANALYSIS_IDS.append(row.id)
    return row


def _modal(
    *,
    org_id: str,
    billed_user_id: str,
    cost_usd: str | None,
    finished_at: datetime | None,
    deleted_at: datetime | None = None,
) -> ModalCostSpanModel:
    started = (finished_at or datetime.now(timezone.utc)) - timedelta(minutes=1)
    row = ModalCostSpanModel(
        id=f"mcomp-{_RUN}-{uuid.uuid4().hex[:8]}",
        org_id=org_id,
        billed_user_id=billed_user_id,
        worker_job_id=f"mcomp-job-{_RUN}-{uuid.uuid4().hex[:6]}",
        worker_job_attempt=1,
        component_role="agent_sandbox",
        span_ordinal=0,
        provider="daytona",
        started_at=started,
        finished_at=finished_at,
        basis="hooks",
        spec_source="pinned",
        cost_usd=Decimal(cost_usd) if cost_usd is not None else None,
        cost_source="estimated",
        estimator_version=1,
        deleted_at=deleted_at,
    )
    _MODAL_IDS.append(row.id)
    return row


async def _make_billed_task(cleanup_task_ids, *, n_trials, billed_user, org_id):
    task_id = f"quota-comp-{_RUN}-{uuid.uuid4().hex[:6]}"
    cleanup_task_ids.append(task_id)
    await _ensure_org(org_id)
    async with get_session() as session:
        await create_task(
            session,
            _submission("quota-comp", n_trials=n_trials),
            task_id=task_id,
            org_id=org_id,
            billed_user_id=billed_user,
        )
        await session.flush()
    return task_id


@pytest.mark.asyncio
async def test_composite_spend_ignored_when_setting_false(
    cleanup_task_ids, monkeypatch
):
    monkeypatch.setattr(
        settings, "quota_counts_analysis_and_compute", False, raising=False
    )
    org_id = f"org-comp-{_RUN}-off"
    billed_user = f"user-comp-{_RUN}-off"
    now = datetime.now(timezone.utc)
    task_id = await _make_billed_task(
        cleanup_task_ids, n_trials=1, billed_user=billed_user, org_id=org_id
    )
    async with get_session() as session:
        trial = await session.get(TrialModel, f"{task_id}-0")
        trial.finished_at = now
        trial.cost_usd = 0.10
        session.add_all(
            [
                _analysis(
                    org_id=org_id,
                    billed_user_id=billed_user,
                    cost_usd=1.25,
                    created_at=now,
                ),
                _modal(
                    org_id=org_id,
                    billed_user_id=billed_user,
                    cost_usd="2.50",
                    finished_at=now,
                ),
            ]
        )
        await session.flush()

        user_total = await sum_cost_usd(
            session, org_id, billed_user, quota_window_start(now)
        )
        org_total = await sum_org_cost_usd(session, org_id, start_of_month_utc(now))
        by_user = await sum_cost_usd_by_user(session, org_id, quota_window_start(now))
        by_org_user = await sum_cost_usd_by_org_user_all_orgs(
            session, quota_window_start(now)
        )

    assert user_total == Decimal("0.1000")
    assert org_total == Decimal("0.1000")
    assert by_user[billed_user] == Decimal("0.1000")
    assert by_org_user[(org_id, billed_user)] == Decimal("0.1000")


@pytest.mark.asyncio
async def test_composite_spend_adds_analysis_and_compute_to_user_24h(
    cleanup_task_ids, monkeypatch
):
    monkeypatch.setattr(
        settings, "quota_counts_analysis_and_compute", True, raising=False
    )
    org_id = f"org-comp-{_RUN}-user"
    billed_user = f"user-comp-{_RUN}-user"
    other_user = f"user-comp-{_RUN}-other"
    now = datetime.now(timezone.utc)
    aged_out = now - timedelta(hours=25)
    task_id = await _make_billed_task(
        cleanup_task_ids, n_trials=1, billed_user=billed_user, org_id=org_id
    )
    async with get_session() as session:
        trial = await session.get(TrialModel, f"{task_id}-0")
        trial.finished_at = now
        trial.cost_usd = 0.10
        session.add_all(
            [
                _analysis(
                    org_id=org_id,
                    billed_user_id=billed_user,
                    cost_usd=1.25,
                    created_at=now,
                ),
                _modal(
                    org_id=org_id,
                    billed_user_id=billed_user,
                    cost_usd="2.50",
                    finished_at=now,
                ),
                _analysis(
                    org_id=org_id,
                    billed_user_id=other_user,
                    cost_usd=9.99,
                    created_at=now,
                ),
                _analysis(
                    org_id=org_id,
                    billed_user_id=billed_user,
                    cost_usd=8.00,
                    created_at=aged_out,
                ),
                _modal(
                    org_id=org_id,
                    billed_user_id=billed_user,
                    cost_usd="7.00",
                    finished_at=aged_out,
                ),
            ]
        )
        await session.flush()

        user_total = await sum_cost_usd(
            session, org_id, billed_user, quota_window_start(now)
        )
        by_user = await sum_cost_usd_by_user(session, org_id, quota_window_start(now))
        by_org_user = await sum_cost_usd_by_org_user_all_orgs(
            session, quota_window_start(now)
        )

    assert user_total == Decimal("3.8500")
    assert by_user[billed_user] == Decimal("3.8500")
    assert by_user[other_user] == Decimal("9.9900")
    assert by_org_user[(org_id, billed_user)] == Decimal("3.8500")


@pytest.mark.asyncio
async def test_composite_spend_adds_analysis_and_compute_to_org_month(
    cleanup_task_ids, monkeypatch
):
    monkeypatch.setattr(
        settings, "quota_counts_analysis_and_compute", True, raising=False
    )
    org_id = f"org-comp-{_RUN}-org"
    billed_user = f"user-comp-{_RUN}-org"
    now = datetime.now(timezone.utc)
    before_month = start_of_month_utc(now) - timedelta(seconds=1)
    task_id = await _make_billed_task(
        cleanup_task_ids, n_trials=1, billed_user=billed_user, org_id=org_id
    )
    async with get_session() as session:
        trial = await session.get(TrialModel, f"{task_id}-0")
        trial.finished_at = now
        trial.cost_usd = 0.10
        session.add_all(
            [
                _analysis(
                    org_id=org_id,
                    billed_user_id=billed_user,
                    cost_usd=1.25,
                    created_at=now,
                ),
                _modal(
                    org_id=org_id,
                    billed_user_id=billed_user,
                    cost_usd="2.50",
                    finished_at=now,
                ),
                _analysis(
                    org_id=org_id,
                    billed_user_id=billed_user,
                    cost_usd=4.00,
                    created_at=before_month,
                ),
                _modal(
                    org_id=org_id,
                    billed_user_id=billed_user,
                    cost_usd="5.00",
                    finished_at=before_month,
                ),
            ]
        )
        await session.flush()

        org_total = await sum_org_cost_usd(session, org_id, start_of_month_utc(now))

    assert org_total == Decimal("3.8500")


@pytest.mark.asyncio
async def test_composite_spend_excludes_soft_deleted_analysis_and_compute(
    cleanup_task_ids, monkeypatch
):
    monkeypatch.setattr(
        settings, "quota_counts_analysis_and_compute", True, raising=False
    )
    org_id = f"org-comp-{_RUN}-del"
    billed_user = f"user-comp-{_RUN}-del"
    now = datetime.now(timezone.utc)
    task_id = await _make_billed_task(
        cleanup_task_ids, n_trials=1, billed_user=billed_user, org_id=org_id
    )
    async with get_session() as session:
        trial = await session.get(TrialModel, f"{task_id}-0")
        trial.finished_at = now
        trial.cost_usd = 0.10
        session.add_all(
            [
                _analysis(
                    org_id=org_id,
                    billed_user_id=billed_user,
                    cost_usd=1.25,
                    created_at=now,
                    deleted_at=now,
                ),
                _modal(
                    org_id=org_id,
                    billed_user_id=billed_user,
                    cost_usd="2.50",
                    finished_at=now,
                    deleted_at=now,
                ),
                _modal(
                    org_id=org_id,
                    billed_user_id=billed_user,
                    cost_usd=None,
                    finished_at=now,
                ),
                _modal(
                    org_id=org_id,
                    billed_user_id=billed_user,
                    cost_usd="3.00",
                    finished_at=None,
                ),
            ]
        )
        await session.flush()

        user_total = await sum_cost_usd(
            session, org_id, billed_user, quota_window_start(now)
        )
        org_total = await sum_org_cost_usd(session, org_id, start_of_month_utc(now))

    assert user_total == Decimal("0.1000")
    assert org_total == Decimal("0.1000")


@pytest.mark.asyncio
async def test_effective_limits_include_live_bump_without_quota_row(
    cleanup_task_ids, monkeypatch
):
    monkeypatch.setattr(settings, "default_daily_quota_usd", Decimal("200.00"))
    org_id = f"org-comp-{_RUN}-bump"
    user_id = f"user-comp-{_RUN}-bump"
    await _ensure_user(org_id, user_id)
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO quota_bumps "
                "(id, org_id, user_id, amount_usd, expires_at, created_at, updated_at) "
                "VALUES (:id, :org_id, :user_id, :amount, NOW() + INTERVAL '6 hours', "
                "NOW(), NOW())"
            ),
            {
                "id": f"bump-{_RUN}",
                "org_id": org_id,
                "user_id": user_id,
                "amount": Decimal("40.0000"),
            },
        )
        await session.flush()
        limits = await effective_limits_by_org_user_all_orgs(session)

    assert limits[(org_id, user_id)] == to_money_decimal(Decimal("240.00"))

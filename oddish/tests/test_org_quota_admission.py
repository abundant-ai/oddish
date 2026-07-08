from __future__ import annotations

import logging
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import QuotaMode, settings  # noqa: E402
from oddish.core.quota_admission import (  # noqa: E402
    OrgQuotaExceeded,
    QuotaExceeded,
    Unattributed,
    admit_trials,
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
        task_path="s3://test-bucket/org-quota-admission-fake-task",
        trials=[TrialSpec(agent="nop", model=None) for _ in range(n_trials)],
    )


_ORG_IDS: set[str] = set()


async def _ensure_org(org_id: str) -> None:
    """Seed the organizations row that tasks.org_id / org_quotas.org_id FK to
    (the combined oddish+backend dev DB enforces those FKs)."""
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


@pytest.fixture(autouse=True)
def _enforce_mode(monkeypatch):
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.ENFORCE)
    monkeypatch.setattr(settings, "pending_trial_reservation_usd", Decimal("0"))
    # Per-user cap effectively off by default so the ORG cap is what bites,
    # unless a test overrides it.
    monkeypatch.setattr(settings, "default_daily_quota_usd", Decimal("1000"))
    monkeypatch.setattr(settings, "default_org_monthly_quota_usd", Decimal("0.30"))


async def _make_billed_task(cleanup_task_ids, *, n_trials, billed_user, org_id):
    unique = uuid.uuid4().hex[:6]
    task_id = f"org-adm-{_RUN}-{unique}"
    cleanup_task_ids.append(task_id)
    await _ensure_org(org_id)
    async with get_session() as session:
        await create_task(
            session,
            _submission(f"org-adm-{unique}", n_trials=n_trials),
            task_id=task_id,
            org_id=org_id,
            billed_user_id=billed_user,
        )
        await session.flush()
    return task_id


async def _settle(task_id, index, cost_usd, *, now=None):
    now = now or datetime.now(timezone.utc)
    async with get_session() as session:
        trial = await session.get(TrialModel, f"{task_id}-{index}")
        trial.finished_at = now
        trial.cost_usd = cost_usd


# --- org gate blocks in ENFORCE when the org total reaches the cap --------------


@pytest.mark.asyncio
async def test_org_gate_blocks_in_enforce(cleanup_task_ids):
    org_id = f"orgA-{_RUN}-{uuid.uuid4().hex[:6]}"
    task_id = await _make_billed_task(
        cleanup_task_ids, n_trials=2, billed_user=f"user-a-{_RUN}", org_id=org_id
    )
    await _settle(task_id, 0, 0.10)
    await _settle(task_id, 1, 0.20)  # org total 0.30 >= 0.30 cap

    async with get_session() as session:
        with pytest.raises(OrgQuotaExceeded) as raised:
            await admit_trials(session, org_id, f"user-a-{_RUN}", count=1)
    assert raised.value.status_code == 402
    assert raised.value.detail["used_usd"] == pytest.approx(0.30)
    assert raised.value.detail["limit_usd"] == pytest.approx(0.30)


@pytest.mark.asyncio
async def test_org_gate_allows_when_org_under_budget(cleanup_task_ids):
    org_id = f"orgB-{_RUN}-{uuid.uuid4().hex[:6]}"
    task_id = await _make_billed_task(
        cleanup_task_ids, n_trials=2, billed_user=f"user-b-{_RUN}", org_id=org_id
    )
    await _settle(task_id, 0, 0.10)
    await _settle(task_id, 1, 0.19)  # org total 0.29 < 0.30 cap

    async with get_session() as session:
        await admit_trials(session, org_id, f"user-b-{_RUN}", count=1)


# --- no org cap configured -> the org gate never blocks ------------------------


@pytest.mark.asyncio
async def test_org_gate_noop_when_no_org_limit(cleanup_task_ids, monkeypatch):
    monkeypatch.setattr(settings, "default_org_monthly_quota_usd", None)
    org_id = f"orgC-{_RUN}-{uuid.uuid4().hex[:6]}"
    task_id = await _make_billed_task(
        cleanup_task_ids, n_trials=1, billed_user=f"user-c-{_RUN}", org_id=org_id
    )
    await _settle(task_id, 0, 999.0)  # far over any cap, but none configured

    async with get_session() as session:
        await admit_trials(session, org_id, f"user-c-{_RUN}", count=1)


# --- shadow: over the org cap logs but does not raise -------------------------


@pytest.mark.asyncio
async def test_org_gate_shadow_does_not_raise(cleanup_task_ids, monkeypatch, caplog):
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.SHADOW)
    org_id = f"orgD-{_RUN}-{uuid.uuid4().hex[:6]}"
    task_id = await _make_billed_task(
        cleanup_task_ids, n_trials=1, billed_user=f"user-d-{_RUN}", org_id=org_id
    )
    await _settle(task_id, 0, 5.00)

    with caplog.at_level(logging.WARNING):
        async with get_session() as session:
            await admit_trials(session, org_id, f"user-d-{_RUN}", count=1)
    assert "reason=org_over_budget" in caplog.text


@pytest.mark.asyncio
async def test_org_gate_off_is_noop(cleanup_task_ids, monkeypatch):
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.OFF)
    org_id = f"orgE-{_RUN}-{uuid.uuid4().hex[:6]}"
    task_id = await _make_billed_task(
        cleanup_task_ids, n_trials=1, billed_user=f"user-e-{_RUN}", org_id=org_id
    )
    await _settle(task_id, 0, 999.0)
    async with get_session() as session:
        await admit_trials(session, org_id, f"user-e-{_RUN}", count=1)


# --- unattributed submissions in shadow STILL run the org check ---------------


@pytest.mark.asyncio
async def test_unattributed_shadow_still_runs_org_check(
    cleanup_task_ids, monkeypatch, caplog
):
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.SHADOW)
    org_id = f"orgF-{_RUN}-{uuid.uuid4().hex[:6]}"
    task_id = await _make_billed_task(
        cleanup_task_ids, n_trials=1, billed_user=f"user-f-{_RUN}", org_id=org_id
    )
    await _settle(task_id, 0, 5.00)  # org over the 0.30 cap

    with caplog.at_level(logging.WARNING):
        async with get_session() as session:
            # billed_user_id=None: today this logged "unattributed" and returned;
            # the org check must still run for the NULL-billed submission.
            await admit_trials(session, org_id, None, count=1)
    assert "reason=unattributed" in caplog.text
    assert "reason=org_over_budget" in caplog.text


@pytest.mark.asyncio
async def test_unattributed_enforce_raises_before_org_check(cleanup_task_ids):
    org_id = f"orgG-{_RUN}-{uuid.uuid4().hex[:6]}"
    async with get_session() as session:
        with pytest.raises(Unattributed):
            await admit_trials(session, org_id, None, count=1)


# --- legacy-retry carve-out: allow_unattributed skips the 403, NOT the org cap ---


@pytest.mark.asyncio
async def test_allow_unattributed_still_hits_org_cap_in_enforce(cleanup_task_ids):
    # A NULL-billed legacy retry is exempt from the linkage 403, but its spend
    # counts toward the org total -- so an over-cap org must still 402 it.
    org_id = f"orgJ-{_RUN}-{uuid.uuid4().hex[:6]}"
    task_id = await _make_billed_task(
        cleanup_task_ids, n_trials=1, billed_user=None, org_id=org_id
    )
    await _settle(task_id, 0, 5.00)  # NULL-billed spend over the 0.30 cap

    async with get_session() as session:
        with pytest.raises(OrgQuotaExceeded):
            await admit_trials(session, org_id, None, count=1, allow_unattributed=True)


@pytest.mark.asyncio
async def test_allow_unattributed_admits_when_org_under_cap(cleanup_task_ids):
    org_id = f"orgK-{_RUN}-{uuid.uuid4().hex[:6]}"
    task_id = await _make_billed_task(
        cleanup_task_ids, n_trials=1, billed_user=None, org_id=org_id
    )
    await _settle(task_id, 0, 0.10)  # under the 0.30 cap

    async with get_session() as session:
        # No Unattributed 403, no OrgQuotaExceeded 402.
        await admit_trials(session, org_id, None, count=1, allow_unattributed=True)


# --- per-user and org caps are independent ------------------------------------


@pytest.mark.asyncio
async def test_per_user_block_fires_when_org_under_budget(
    cleanup_task_ids, monkeypatch
):
    # Tight per-user cap, generous org cap: the per-user gate must still bite.
    monkeypatch.setattr(settings, "default_daily_quota_usd", Decimal("0.30"))
    monkeypatch.setattr(settings, "default_org_monthly_quota_usd", Decimal("1000"))
    org_id = f"orgH-{_RUN}-{uuid.uuid4().hex[:6]}"
    billed_user = f"user-h-{_RUN}"
    task_id = await _make_billed_task(
        cleanup_task_ids, n_trials=1, billed_user=billed_user, org_id=org_id
    )
    await _settle(task_id, 0, 0.30)

    async with get_session() as session:
        with pytest.raises(QuotaExceeded):
            await admit_trials(session, org_id, billed_user, count=1)


@pytest.mark.asyncio
async def test_org_block_fires_when_user_under_budget(cleanup_task_ids, monkeypatch):
    # Generous per-user cap; org cap bites on the SUM across two payers.
    monkeypatch.setattr(settings, "default_daily_quota_usd", Decimal("1000"))
    monkeypatch.setattr(settings, "default_org_monthly_quota_usd", Decimal("0.30"))
    org_id = f"orgI-{_RUN}-{uuid.uuid4().hex[:6]}"
    user_a = f"user-i-a-{_RUN}"
    user_b = f"user-i-b-{_RUN}"
    task_a = await _make_billed_task(
        cleanup_task_ids, n_trials=1, billed_user=user_a, org_id=org_id
    )
    task_b = await _make_billed_task(
        cleanup_task_ids, n_trials=1, billed_user=user_b, org_id=org_id
    )
    await _settle(task_a, 0, 0.10)  # user_a is well under their own cap
    await _settle(task_b, 0, 0.20)  # org total 0.30 >= 0.30

    async with get_session() as session:
        # Admitting user_a (0.10 << 1000 personal) must still block on the org SUM.
        with pytest.raises(OrgQuotaExceeded):
            await admit_trials(session, org_id, user_a, count=1)

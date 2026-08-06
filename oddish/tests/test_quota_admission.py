from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import QuotaMode, settings  # noqa: E402
from oddish.core.quota_admission import (  # noqa: E402
    QuotaExceeded,
    Unattributed,
    admit_trials,
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
        task_path="s3://test-bucket/quota-admission-fake-task",
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


@pytest.fixture(autouse=True)
def _enforce_mode(monkeypatch):
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.ENFORCE)
    monkeypatch.setattr(settings, "pending_trial_reservation_usd", Decimal("0"))
    monkeypatch.setattr(settings, "default_daily_quota_usd", Decimal("0.3000"))


async def _make_billed_task(cleanup_task_ids, *, n_trials, billed_user, org_id):
    task_id = f"quota-adm-{_RUN}-{uuid.uuid4().hex[:6]}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        await create_task(
            session,
            _submission("quota-adm", n_trials=n_trials),
            task_id=task_id,
            org_id=org_id,
            billed_user_id=billed_user,
        )
        await session.flush()
    return task_id


@pytest.mark.asyncio
async def test_enforced_admission_acquires_shared_quota_locks(monkeypatch):
    from oddish.core import quota_admission

    calls = []

    async def record_locks(session, org_id, billed_user_id):
        calls.append((session, org_id, billed_user_id))

    async def no_op(*_args, **_kwargs):
        return None

    fake_session = object()
    monkeypatch.setattr(quota_admission, "acquire_quota_locks", record_locks)
    monkeypatch.setattr(quota_admission, "_check_user_quota", no_op)
    monkeypatch.setattr(quota_admission, "_check_org_quota", no_op)

    await admit_trials(fake_session, "org-lock", "user-lock", count=1)

    assert calls == [(fake_session, "org-lock", "user-lock")]


async def _settle(task_id, index, cost_usd, *, now=None):
    now = now or datetime.now(timezone.utc)
    async with get_session() as session:
        trial = await session.get(TrialModel, f"{task_id}-{index}")
        trial.finished_at = now
        trial.cost_usd = cost_usd


@pytest.mark.asyncio
async def test_admit_blocks_at_exactly_the_cap(cleanup_task_ids):
    org_id = f"org-adm-{_RUN}-a"
    billed_user = f"user-adm-{_RUN}-a"
    task_id = await _make_billed_task(
        cleanup_task_ids, n_trials=2, billed_user=billed_user, org_id=org_id
    )
    await _settle(task_id, 0, 0.10)
    await _settle(task_id, 1, 0.20)

    async with get_session() as session:
        with pytest.raises(QuotaExceeded) as raised:
            await admit_trials(session, org_id, billed_user, count=1)
    assert raised.value.status_code == 402
    assert raised.value.detail["used_usd"] == pytest.approx(0.30)
    assert raised.value.detail["reserved_usd"] == pytest.approx(0.0)
    assert raised.value.detail["limit_usd"] == pytest.approx(0.30)


@pytest.mark.asyncio
async def test_admit_allows_just_under_the_cap(cleanup_task_ids):
    org_id = f"org-adm-{_RUN}-b"
    billed_user = f"user-adm-{_RUN}-b"
    task_id = await _make_billed_task(
        cleanup_task_ids, n_trials=2, billed_user=billed_user, org_id=org_id
    )
    await _settle(task_id, 0, 0.10)
    await _settle(task_id, 1, 0.19)

    async with get_session() as session:
        await admit_trials(session, org_id, billed_user, count=1)


@pytest.mark.asyncio
async def test_admit_ignores_spend_finished_at_the_rolling_boundary(
    cleanup_task_ids, monkeypatch
):
    org_id = f"org-adm-{_RUN}-boundary"
    billed_user = f"user-adm-{_RUN}-boundary"
    task_id = await _make_billed_task(
        cleanup_task_ids, n_trials=2, billed_user=billed_user, org_id=org_id
    )
    boundary = datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "oddish.core.quota_admission.quota_window_start", lambda: boundary
    )
    await _settle(task_id, 0, 0.30, now=boundary)

    async with get_session() as session:
        await admit_trials(session, org_id, billed_user, count=1)

    await _settle(task_id, 1, 0.30, now=boundary + timedelta(microseconds=1))

    async with get_session() as session:
        with pytest.raises(QuotaExceeded):
            await admit_trials(session, org_id, billed_user, count=1)


@pytest.mark.asyncio
async def test_null_billed_user_raises_unattributed_in_enforce():
    async with get_session() as session:
        with pytest.raises(Unattributed) as raised:
            await admit_trials(session, "org-x", None, count=1)
    assert raised.value.status_code == 403


@pytest.mark.asyncio
async def test_null_billed_user_is_silent_in_shadow(monkeypatch):
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.SHADOW)
    async with get_session() as session:
        await admit_trials(session, "org-x", None, count=1)


@pytest.mark.asyncio
async def test_off_mode_never_blocks(cleanup_task_ids, monkeypatch):
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.OFF)
    org_id = f"org-adm-{_RUN}-c"
    billed_user = f"user-adm-{_RUN}-c"
    task_id = await _make_billed_task(
        cleanup_task_ids, n_trials=1, billed_user=billed_user, org_id=org_id
    )
    await _settle(task_id, 0, 999.0)
    async with get_session() as session:
        await admit_trials(session, org_id, billed_user, count=1)


@pytest.mark.asyncio
async def test_oss_org_none_never_blocks():
    async with get_session() as session:
        await admit_trials(session, None, None, count=5)


@pytest.mark.asyncio
async def test_missing_quota_row_enforces_at_default(cleanup_task_ids):
    org_id = f"org-adm-{_RUN}-d"
    billed_user = f"user-adm-{_RUN}-d"
    task_id = await _make_billed_task(
        cleanup_task_ids, n_trials=1, billed_user=billed_user, org_id=org_id
    )
    await _settle(task_id, 0, 0.50)

    async with get_session() as session:
        with pytest.raises(QuotaExceeded):
            await admit_trials(session, org_id, billed_user, count=1)


@pytest.mark.asyncio
async def test_inflight_trials_count_toward_reservation(cleanup_task_ids, monkeypatch):
    monkeypatch.setattr(settings, "pending_trial_reservation_usd", Decimal("0.20"))
    org_id = f"org-adm-{_RUN}-e"
    billed_user = f"user-adm-{_RUN}-e"
    await _make_billed_task(
        cleanup_task_ids, n_trials=2, billed_user=billed_user, org_id=org_id
    )
    async with get_session() as session:
        with pytest.raises(QuotaExceeded) as raised:
            await admit_trials(session, org_id, billed_user, count=1)
    assert raised.value.detail["reserved_usd"] == pytest.approx(0.60)


@pytest.mark.asyncio
async def test_retrying_attempt_cost_counts_toward_reservation(
    cleanup_task_ids, monkeypatch
):
    monkeypatch.setattr(settings, "pending_trial_reservation_usd", Decimal("0.20"))
    org_id = f"org-adm-{_RUN}-g"
    billed_user = f"user-adm-{_RUN}-g"
    task_id = await _make_billed_task(
        cleanup_task_ids, n_trials=1, billed_user=billed_user, org_id=org_id
    )
    async with get_session() as session:
        retrying = await session.get(TrialModel, f"{task_id}-0")
        retrying.status = TrialStatus.RETRYING
        retrying.cost_usd = 3.0

    async with get_session() as session:
        with pytest.raises(QuotaExceeded) as raised:
            await admit_trials(session, org_id, billed_user, count=1)
    assert raised.value.detail["reserved_usd"] == pytest.approx(3.20)

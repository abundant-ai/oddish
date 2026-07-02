from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import QuotaMode, settings  # noqa: E402
from oddish.core.quota_admission import (  # noqa: E402
    QuotaExceeded,
    Unattributed,
    acquire_payer_lock,
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


async def _settle(task_id, index, cost_usd, *, now=None):
    now = now or datetime.now(timezone.utc)
    async with get_session() as session:
        trial = await session.get(TrialModel, f"{task_id}-{index}")
        trial.finished_at = now
        trial.cost_usd = cost_usd


# --- S5-T1: blocks at exactly limit_usd; admits just under (Decimal boundary) ---


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


# --- S5-T2: unattributed -> 403 in enforce, silent in shadow ------------------


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


# --- S5-T3: off is a no-op; OSS (org_id None) is a no-op ----------------------


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


# --- S5-T4: a missing quota row enforces at the default -----------------------


@pytest.mark.asyncio
async def test_missing_quota_row_enforces_at_default(cleanup_task_ids):
    org_id = f"org-adm-{_RUN}-d"
    billed_user = f"user-adm-{_RUN}-d"
    task_id = await _make_billed_task(
        cleanup_task_ids, n_trials=1, billed_user=billed_user, org_id=org_id
    )
    await _settle(task_id, 0, 0.50)  # over the 0.30 default, no quota row exists

    async with get_session() as session:
        with pytest.raises(QuotaExceeded):
            await admit_trials(session, org_id, billed_user, count=1)


# --- S5-T5: in-flight trials add to the reservation ---------------------------


@pytest.mark.asyncio
async def test_inflight_trials_count_toward_reservation(cleanup_task_ids, monkeypatch):
    monkeypatch.setattr(settings, "pending_trial_reservation_usd", Decimal("0.20"))
    org_id = f"org-adm-{_RUN}-e"
    billed_user = f"user-adm-{_RUN}-e"
    # two QUEUED (in-flight, finished_at NULL) trials, no settled spend
    await _make_billed_task(
        cleanup_task_ids, n_trials=2, billed_user=billed_user, org_id=org_id
    )
    async with get_session() as session:
        # used=0, reserved=(inflight 2 + count 1) * 0.20 = 0.60 >= 0.30 default
        with pytest.raises(QuotaExceeded) as raised:
            await admit_trials(session, org_id, billed_user, count=1)
    assert raised.value.detail["reserved_usd"] == pytest.approx(0.60)


# --- concurrent same-payer admissions serialize on the advisory lock ----------


@pytest.mark.asyncio
async def test_concurrent_admissions_serialize_on_the_payer_lock(
    cleanup_task_ids, monkeypatch
):
    """A admits + inserts but hasn't committed; B (same payer) must wait on the
    advisory xact lock and then see A's inflight row -> 402. Without the lock B
    reads pre-commit state (0 inflight) and both submissions pass."""
    monkeypatch.setattr(settings, "pending_trial_reservation_usd", Decimal("0.20"))
    org_id = f"org-adm-{_RUN}-f"
    billed_user = f"user-adm-{_RUN}-f"
    task_id = f"quota-adm-{_RUN}-{uuid.uuid4().hex[:6]}"
    cleanup_task_ids.append(task_id)

    a_admitted = asyncio.Event()
    a_release = asyncio.Event()

    async def first_submission():
        async with get_session() as session:
            # reserved=(0 inflight + 1) * 0.20 = 0.20 < 0.30 default -> admitted
            await admit_trials(session, org_id, billed_user, count=1)
            await create_task(
                session,
                _submission("quota-adm", n_trials=1),
                task_id=task_id,
                org_id=org_id,
                billed_user_id=billed_user,
            )
            await session.flush()
            a_admitted.set()
            await a_release.wait()
        # get_session commits on exit, releasing the xact lock

    async def second_submission():
        async with get_session() as session:
            # reserved=(1 inflight + 1) * 0.20 = 0.40 >= 0.30 -> blocked
            with pytest.raises(QuotaExceeded):
                await admit_trials(session, org_id, billed_user, count=1)

    first = asyncio.create_task(first_submission())
    await asyncio.wait_for(a_admitted.wait(), timeout=10)
    second = asyncio.create_task(second_submission())
    await asyncio.sleep(0.2)  # let B reach (and block on) the advisory lock
    a_release.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=30)


# --- acquire_payer_lock: escape hatches mirror admit_trials -------------------


class _RecordingSession:
    def __init__(self):
        self.calls = []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params))


@pytest.mark.asyncio
async def test_acquire_payer_lock_escape_hatches(monkeypatch):
    recording = _RecordingSession()

    monkeypatch.setattr(settings, "quota_mode", QuotaMode.OFF)
    await acquire_payer_lock(recording, "org-x", "user-x")
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.SHADOW)
    await acquire_payer_lock(recording, "org-x", "user-x")
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.ENFORCE)
    await acquire_payer_lock(recording, None, "user-x")
    await acquire_payer_lock(recording, "org-x", None)
    assert recording.calls == []  # OFF / SHADOW / no org / no payer never lock

    await acquire_payer_lock(recording, "org-x", "user-x")
    assert len(recording.calls) == 1
    sql, params = recording.calls[0]
    assert "pg_advisory_xact_lock" in sql
    assert params == {"k": "quota:org-x:user-x"}


# --- M4: an in-flight retry's accumulated cost reserves at full value ---------


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
        retrying.cost_usd = 3.0  # attempt-1 burn, preserved across re-claim

    async with get_session() as session:
        # reserved = max(3.00, 0.20) inflight + 1 * 0.20 = 3.20 >= 0.30 default
        with pytest.raises(QuotaExceeded) as raised:
            await admit_trials(session, org_id, billed_user, count=1)
    assert raised.value.detail["reserved_usd"] == pytest.approx(3.20)

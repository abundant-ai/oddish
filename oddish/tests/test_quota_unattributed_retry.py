from __future__ import annotations

import logging
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
    Unattributed,
    UnattributedPoolExceeded,
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
        task_path="s3://test-bucket/quota-unattrib-fake-task",
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
    monkeypatch.setattr(settings, "default_org_monthly_quota_usd", None)
    # The pool ceiling is its own knob and ships unset; these tests opt in.
    monkeypatch.setattr(settings, "unattributed_pool_limit_usd", Decimal("0.3000"))


async def _make_billed_task(cleanup_task_ids, *, n_trials, billed_user, org_id):
    task_id = f"quota-unattrib-{_RUN}-{uuid.uuid4().hex[:6]}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        await create_task(
            session,
            _submission("quota-unattrib", n_trials=n_trials),
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


@pytest.mark.asyncio
async def test_fresh_submit_unattributed_still_403s_in_enforce():
    async with get_session() as session:
        with pytest.raises(Unattributed) as raised:
            await admit_trials(session, "org-x", None, count=1)
    assert raised.value.status_code == 403


@pytest.mark.asyncio
async def test_unattributed_retry_admitted_under_pooled_ceiling(
    cleanup_task_ids, caplog
):
    org_id = f"org-unattrib-{_RUN}-under"
    task_id = await _make_billed_task(
        cleanup_task_ids, n_trials=1, billed_user=None, org_id=org_id
    )
    await _settle(task_id, 0, 0.19)

    with caplog.at_level(logging.WARNING):
        async with get_session() as session:
            await admit_trials(session, org_id, None, count=1, allow_unattributed=True)
    assert "reason=unattributed_admitted" in caplog.text
    assert f"org_id={org_id}" in caplog.text


@pytest.mark.asyncio
async def test_unattributed_retry_blocked_over_pooled_ceiling(cleanup_task_ids, caplog):
    org_id = f"org-unattrib-{_RUN}-over"
    task_id = await _make_billed_task(
        cleanup_task_ids, n_trials=1, billed_user=None, org_id=org_id
    )
    await _settle(task_id, 0, 0.30)

    with caplog.at_level(logging.WARNING):
        async with get_session() as session:
            with pytest.raises(UnattributedPoolExceeded) as raised:
                await admit_trials(
                    session, org_id, None, count=1, allow_unattributed=True
                )
    # A blocked admission must not also claim it was admitted.
    assert "reason=unattributed_admitted" not in caplog.text
    assert raised.value.status_code == 402
    assert raised.value.detail["used_usd"] == pytest.approx(0.30)
    assert raised.value.detail["limit_usd"] == pytest.approx(0.30)
    # Must not tell the caller to get "your quota" raised: no per-user override
    # can lift a pool, so that advice would send them somewhere with no lever.
    assert "raising an individual quota" in raised.value.detail["message"]


@pytest.mark.asyncio
async def test_unattributed_pool_uncapped_by_default(cleanup_task_ids, monkeypatch):
    # The shipped default (None) must admit regardless of pooled spend, so this
    # change cannot start blocking retries until an operator sets a ceiling.
    monkeypatch.setattr(settings, "unattributed_pool_limit_usd", None)
    org_id = f"org-unattrib-{_RUN}-uncapped"
    task_id = await _make_billed_task(
        cleanup_task_ids, n_trials=1, billed_user=None, org_id=org_id
    )
    await _settle(task_id, 0, 9_999.0)

    async with get_session() as session:
        await admit_trials(session, org_id, None, count=1, allow_unattributed=True)


@pytest.mark.asyncio
async def test_unattributed_retry_off_never_blocks(cleanup_task_ids, monkeypatch):
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.OFF)
    org_id = f"org-unattrib-{_RUN}-off"
    task_id = await _make_billed_task(
        cleanup_task_ids, n_trials=1, billed_user=None, org_id=org_id
    )
    await _settle(task_id, 0, 999.0)
    async with get_session() as session:
        await admit_trials(session, org_id, None, count=1, allow_unattributed=True)
        await admit_trials(session, org_id, None, count=1)


@pytest.mark.asyncio
async def test_unattributed_retry_shadow_logs_and_never_blocks(
    cleanup_task_ids, monkeypatch, caplog
):
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.SHADOW)
    org_id = f"org-unattrib-{_RUN}-shadow"
    task_id = await _make_billed_task(
        cleanup_task_ids, n_trials=1, billed_user=None, org_id=org_id
    )
    await _settle(task_id, 0, 5.00)

    with caplog.at_level(logging.WARNING):
        async with get_session() as session:
            await admit_trials(session, org_id, None, count=1, allow_unattributed=True)
    assert "reason=unattributed_over_budget" in caplog.text
    assert "metric=quota.would_block" in caplog.text

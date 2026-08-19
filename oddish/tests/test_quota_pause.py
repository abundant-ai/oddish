from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from oddish.config import QuotaMode, settings
from oddish.core import quota_admission, quota_pause
from oddish.core.quota_admission import QuotaPaused
from oddish.core.quotas import quota_pause_limit_usd
from oddish.workers.harbor import quota_control


def test_pause_limit_uses_five_percent_remaining(monkeypatch):
    monkeypatch.setattr(settings, "quota_pause_remaining_percent", Decimal("5"))
    monkeypatch.setattr(settings, "quota_pause_remaining_usd", None)

    assert quota_pause_limit_usd(Decimal("200")) == Decimal("190")


def test_absolute_reserve_wins_when_larger(monkeypatch):
    monkeypatch.setattr(settings, "quota_pause_remaining_percent", Decimal("5"))
    monkeypatch.setattr(settings, "quota_pause_remaining_usd", Decimal("25"))

    assert quota_pause_limit_usd(Decimal("200")) == Decimal("175")


def test_zero_reserve_disables_pause(monkeypatch):
    monkeypatch.setattr(settings, "quota_pause_remaining_percent", Decimal(0))
    monkeypatch.setattr(settings, "quota_pause_remaining_usd", None)

    assert quota_pause_limit_usd(Decimal("200")) is None


@pytest.mark.asyncio
async def test_user_pause_uses_settled_and_reported_spend(monkeypatch):
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.ENFORCE)
    monkeypatch.setattr(settings, "quota_pause_remaining_percent", Decimal("5"))
    monkeypatch.setattr(settings, "quota_pause_remaining_usd", None)
    monkeypatch.setattr(
        quota_pause, "get_effective_org_limit", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        quota_pause, "get_effective_limit", AsyncMock(return_value=Decimal("200"))
    )
    monkeypatch.setattr(
        quota_pause, "sum_cost_usd", AsyncMock(return_value=Decimal("185"))
    )
    monkeypatch.setattr(
        quota_pause, "inflight_reported_usd", AsyncMock(return_value=Decimal("5"))
    )

    requested = await quota_pause.quota_pause_requested(
        object(), org_id="org-1", billed_user_id="user-1"
    )

    assert requested is True


@pytest.mark.asyncio
async def test_admission_closes_at_pause_limit(monkeypatch):
    monkeypatch.setattr(settings, "quota_pause_remaining_percent", Decimal("5"))
    monkeypatch.setattr(settings, "quota_pause_remaining_usd", None)
    monkeypatch.setattr(
        quota_admission, "get_effective_limit", AsyncMock(return_value=Decimal("200"))
    )
    monkeypatch.setattr(
        quota_admission, "sum_cost_usd", AsyncMock(return_value=Decimal("190"))
    )
    monkeypatch.setattr(
        quota_admission, "inflight_reported_usd", AsyncMock(return_value=Decimal(0))
    )

    with pytest.raises(QuotaPaused):
        await quota_admission._check_user_quota(
            object(), "org-1", "user-1", 1, enforce=True
        )


@pytest.mark.asyncio
async def test_quota_control_pauses_and_resumes(monkeypatch):
    stop = quota_control.asyncio.Event()
    job = SimpleNamespace(pause=AsyncMock(), resume=AsyncMock())
    monkeypatch.setattr(settings, "quota_pause_poll_seconds", 0.001)
    monkeypatch.setattr(settings, "quota_pause_refresh_seconds", 1000)

    async def finish_after_resume():
        while not job.pause.await_count:
            await quota_control.asyncio.sleep(0)
        quota_control.set_quota_pause_requested("trial-1", False)
        while not job.resume.await_count:
            await quota_control.asyncio.sleep(0)
        stop.set()

    quota_control.set_quota_pause_requested("trial-1", True)
    await quota_control.asyncio.gather(
        quota_control.control_job_quota_pause(
            job,
            trial_id="trial-1",
            org_id="org-1",
            billed_user_id="user-1",
            stop=stop,
        ),
        finish_after_resume(),
    )

    job.pause.assert_awaited_once_with()
    job.resume.assert_awaited_once_with()

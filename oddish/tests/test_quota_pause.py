from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

from oddish.config import QuotaMode, settings
from oddish.core import quota_admission, quota_pause
from oddish.core.quota_admission import QuotaPaused
from oddish.core.quotas import quota_pause_limit_usd
from oddish.workers.harbor import quota_control


@asynccontextmanager
async def _empty_session():
    yield object()


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
async def test_record_pause_state_preserves_worker_ownership(monkeypatch):
    trial = SimpleNamespace(
        status=quota_control.TrialStatus.RUNNING,
        current_worker_id="worker-1",
        current_queue_slot=3,
        heartbeat_at="heartbeat",
    )
    session = SimpleNamespace(get=AsyncMock(return_value=trial))

    @asynccontextmanager
    async def fake_get_session():
        yield session

    monkeypatch.setattr(quota_control, "get_session", fake_get_session)

    await quota_control._record_pause_state("trial-1", True)
    assert trial.status == quota_control.TrialStatus.PAUSED

    await quota_control._record_pause_state("trial-1", False)
    assert trial.status == quota_control.TrialStatus.RUNNING
    assert trial.current_worker_id == "worker-1"
    assert trial.current_queue_slot == 3
    assert trial.heartbeat_at == "heartbeat"
    assert session.get.await_args_list == [
        call(quota_control.TrialModel, "trial-1", with_for_update=True),
        call(quota_control.TrialModel, "trial-1", with_for_update=True),
    ]


@pytest.mark.asyncio
async def test_quota_control_pauses_and_resumes(monkeypatch):
    stop = quota_control.asyncio.Event()
    controller = quota_control.QuotaPauseController()
    job = SimpleNamespace(pause=AsyncMock(), resume=AsyncMock())
    monkeypatch.setattr(settings, "quota_pause_poll_seconds", 0.001)
    monkeypatch.setattr(settings, "quota_pause_refresh_seconds", 1000)
    monkeypatch.setattr(quota_control, "get_session", _empty_session)
    monkeypatch.setattr(
        quota_control, "quota_pause_requested", AsyncMock(return_value=True)
    )
    record_pause_state = AsyncMock()
    monkeypatch.setattr(quota_control, "_record_pause_state", record_pause_state)

    async def finish_after_resume():
        while not job.pause.await_count:
            await quota_control.asyncio.sleep(0)
        controller.requested = False
        while not job.resume.await_count:
            await quota_control.asyncio.sleep(0)
        stop.set()

    await quota_control.asyncio.gather(
        quota_control.control_job_quota_pause(
            job,
            trial_id="trial-1",
            org_id="org-1",
            billed_user_id="user-1",
            stop=stop,
            controller=controller,
        ),
        finish_after_resume(),
    )

    job.pause.assert_awaited_once_with()
    job.resume.assert_awaited_once_with()
    assert record_pause_state.await_args_list == [
        call("trial-1", True),
        call("trial-1", False),
    ]


@pytest.mark.asyncio
async def test_quota_control_polls_while_running(monkeypatch):
    stop = quota_control.asyncio.Event()
    controller = quota_control.QuotaPauseController()
    job = SimpleNamespace(pause=AsyncMock(), resume=AsyncMock())
    monkeypatch.setattr(settings, "quota_pause_poll_seconds", 0.001)
    monkeypatch.setattr(settings, "quota_pause_refresh_seconds", 0.001)
    monkeypatch.setattr(quota_control, "get_session", _empty_session)
    refresh = AsyncMock(return_value=True)
    monkeypatch.setattr(quota_control, "quota_pause_requested", refresh)
    monkeypatch.setattr(quota_control, "_record_pause_state", AsyncMock())

    async def stop_after_pause():
        while not job.pause.await_count:
            await quota_control.asyncio.sleep(0)
        stop.set()

    await quota_control.asyncio.gather(
        quota_control.control_job_quota_pause(
            job,
            trial_id="trial-1",
            org_id="org-1",
            billed_user_id="user-1",
            stop=stop,
            controller=controller,
        ),
        stop_after_pause(),
    )

    refresh.assert_awaited()
    job.pause.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_quota_control_failure_cancels_job(monkeypatch):
    run_cancelled = quota_control.asyncio.Event()

    async def run():
        try:
            await quota_control.asyncio.Event().wait()
        finally:
            run_cancelled.set()

    job = SimpleNamespace(
        run=run,
        pause=AsyncMock(side_effect=RuntimeError("snapshot failed")),
        resume=AsyncMock(),
    )
    monkeypatch.setattr(settings, "quota_pause_poll_seconds", 0.001)
    monkeypatch.setattr(quota_control, "get_session", _empty_session)
    monkeypatch.setattr(
        quota_control, "quota_pause_requested", AsyncMock(return_value=True)
    )

    with pytest.raises(quota_control.QuotaPauseControlError, match="snapshot failed"):
        await quota_control.run_job_with_quota_control(
            job,
            trial_id="trial-1",
            org_id="org-1",
            billed_user_id="user-1",
        )

    assert run_cancelled.is_set()
    assert "trial-1" not in quota_control._controllers


@pytest.mark.asyncio
async def test_quota_control_failure_does_not_wait_forever_for_job(monkeypatch):
    run_cancelled = quota_control.asyncio.Event()
    release_cleanup = quota_control.asyncio.Event()
    cleanup_finished = quota_control.asyncio.Event()

    async def run():
        try:
            await quota_control.asyncio.Event().wait()
        except quota_control.asyncio.CancelledError:
            run_cancelled.set()
            await release_cleanup.wait()
        finally:
            cleanup_finished.set()

    job = SimpleNamespace(
        run=run,
        pause=AsyncMock(side_effect=RuntimeError("snapshot failed")),
        resume=AsyncMock(),
    )
    monkeypatch.setattr(settings, "quota_pause_poll_seconds", 0.001)
    monkeypatch.setattr(settings, "quota_pause_cancel_timeout_seconds", 0.001)
    monkeypatch.setattr(quota_control, "get_session", _empty_session)
    monkeypatch.setattr(
        quota_control, "quota_pause_requested", AsyncMock(return_value=True)
    )

    with pytest.raises(quota_control.QuotaPauseControlError, match="snapshot failed"):
        await quota_control.run_job_with_quota_control(
            job,
            trial_id="trial-1",
            org_id="org-1",
            billed_user_id="user-1",
        )

    assert run_cancelled.is_set()
    assert not cleanup_finished.is_set()
    assert "trial-1" not in quota_control._controllers

    release_cleanup.set()
    await quota_control.asyncio.wait_for(cleanup_finished.wait(), timeout=1)

"""Quota advisory locks: enforcement-only, non-blocking; admission takes none."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from oddish.config import QuotaMode, settings
from oddish.core import quota_enforcement, quotas
from oddish.core.quota_enforcement import (
    QuotaLockBusy,
    cancel_trials_if_quota_reached,
    enforce_trial_quotas,
)


@pytest.mark.asyncio
async def test_try_acquire_quota_locks_returns_false_when_org_lock_busy():
    calls: list[str] = []

    async def fake_scalar(_stmt, params=None):
        key = (params or {}).get("key", "")
        calls.append(key)
        return False

    class _Session:
        async def scalar(self, stmt, params=None):
            return await fake_scalar(stmt, params)

    assert await quotas.try_acquire_quota_locks(_Session(), "org-1", "user-1") is False
    assert calls == ["quota:org:org-1"]


@pytest.mark.asyncio
async def test_try_acquire_quota_locks_requires_user_lock_too():
    async def fake_scalar(_stmt, params=None):
        key = (params or {}).get("key", "")
        return key == "quota:org:org-1"

    class _Session:
        async def scalar(self, stmt, params=None):
            return await fake_scalar(stmt, params)

    assert await quotas.try_acquire_quota_locks(_Session(), "org-1", "user-1") is False
    assert await quotas.try_acquire_quota_locks(_Session(), "org-1", None) is True


@pytest.mark.asyncio
async def test_cancel_raises_when_quota_lock_busy(monkeypatch):
    """Lock contention must not look like under-quota (scope=None)."""
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.ENFORCE)

    async def busy(*_args, **_kwargs):
        return False

    monkeypatch.setattr(quota_enforcement, "try_acquire_quota_locks", busy)

    with pytest.raises(QuotaLockBusy):
        await cancel_trials_if_quota_reached(
            object(),
            org_id="org-busy",
            billed_user_id="user-busy",
        )


@pytest.mark.asyncio
async def test_enforce_returns_none_on_lock_busy_without_settlement(monkeypatch):
    """Settlement / live-tail must retry: None means check did not complete."""
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.ENFORCE)

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    async def busy(*_args, **_kwargs):
        return False

    after_check_calls = 0

    async def after_check():
        nonlocal after_check_calls
        after_check_calls += 1

    monkeypatch.setattr(quota_enforcement, "get_session", fake_get_session)
    monkeypatch.setattr(quota_enforcement, "try_acquire_quota_locks", busy)

    assert (
        await enforce_trial_quotas(
            org_id="org-busy",
            billed_user_id="user-busy",
            caller_trial_id="trial-1",
            after_check=after_check,
        )
        is None
    )
    assert after_check_calls == 0


def test_submission_paths_take_no_quota_advisory_locks():
    """Admission is lock-free; only the enforcement sweep takes quota locks."""
    from oddish.core import quota_admission
    from oddish.core.endpoints import sweep as sweep_mod

    for module in (sweep_mod, quota_admission):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "acquire_quota_locks" not in source.replace(
            "try_acquire_quota_locks", ""
        ), f"{module.__name__} must not take blocking quota advisory locks"
    assert not hasattr(quotas, "acquire_quota_locks")


def test_retry_reserves_trial_index_under_the_task_lock():
    """Concurrent retries/appends must not mint the same ``{task_id}-{N}``."""
    from oddish.core.endpoints import trials as trials_mod

    source = Path(trials_mod.__file__).read_text(encoding="utf-8")
    start = source.index("async def retry_trial_core(")
    end = source.index("\nasync def ", start + 1)
    body = source[start:end]

    lock_at = body.index("with_for_update")
    reserve_at = body.index("await reserve_next_trial_index(")
    assert lock_at < reserve_at, (
        "retry must lock the task row before reserving the trial index"
    )


def test_append_sweep_admits_against_the_locked_plan():
    """An unlocked estimate can 402 after a concurrent append filled the deficit."""
    from oddish.core.endpoints import sweep as sweep_mod

    source = Path(sweep_mod.__file__).read_text(encoding="utf-8")
    assert "async def _plan_append_trials(" in source

    append_start = source.index("if submission.append_to_task:")
    # Limit to the append branch (create mode follows in the same function).
    append_end = source.index("\n    # Create mode", append_start)
    append_body = source[append_start:append_end]

    for_update_at = append_body.index("with_for_update=True")
    plan_at = append_body.index("await _plan_append_trials(")
    admit_at = append_body.index("await admit_trials(")

    assert for_update_at < plan_at < admit_at, (
        "expected FOR UPDATE → plan → admit_trials; "
        f"got for_update@{for_update_at} plan@{plan_at} admit@{admit_at}"
    )
    assert append_body.count("await _plan_append_trials(") == 1, (
        "append must plan once under the task lock, not against an unlocked estimate"
    )
    assert "planned_trials" not in append_body
    assert "count=len(trials)" in append_body[admit_at:]

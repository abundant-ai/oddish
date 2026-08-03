from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from oddish.config import Settings, settings
from oddish.costs.modal_cost import SpanResources
from oddish.db import TrialStatus
from oddish.workers.queue import provider_capacity
from oddish.workers.queue import trial_handler
from oddish.workers.queue import worker_job_single_job


def _resources(*, request: int | None, limit: int | None) -> SpanResources:
    return SpanResources(
        cpu_request=None,
        cpu_limit=None,
        mem_request_mb=request,
        mem_limit_mb=limit,
        gpu_type=None,
        gpu_count=0,
        price_multiplier=1,  # type: ignore[arg-type]
        container_class="sandbox",
        spec_source="pinned",
    )


def test_daytona_capacity_defaults_match_incident_ceiling_and_headroom():
    config = Settings()
    assert config.daytona_capacity_total_memory_mb == 2250 * 1024
    assert config.daytona_capacity_headroom_memory_mb == 450 * 1024
    assert config.daytona_capacity_memory_limit_mb == 1800 * 1024
    assert config.daytona_capacity_max_leases == 384
    assert config.daytona_capacity_default_request_mb == 4096


def test_daytona_capacity_rejects_invalid_headroom(monkeypatch):
    monkeypatch.setenv("ODDISH_DAYTONA_CAPACITY_TOTAL_MEMORY_MB", "4096")
    monkeypatch.setenv("ODDISH_DAYTONA_CAPACITY_HEADROOM_MEMORY_MB", "4096")
    with pytest.raises(ValueError, match="HEADROOM_MEMORY_MB"):
        Settings()


def test_capacity_memory_uses_effective_bound_and_safe_default(monkeypatch):
    monkeypatch.setattr(settings, "daytona_capacity_default_request_mb", 4096)
    assert (
        trial_handler._capacity_memory_mb(_resources(request=2048, limit=8192)) == 8192
    )
    assert (
        trial_handler._capacity_memory_mb(_resources(request=None, limit=None)) == 4096
    )
    assert trial_handler._capacity_memory_mb(None) == 4096


def test_capacity_request_includes_largest_separate_verifier(monkeypatch):
    monkeypatch.setattr(settings, "daytona_capacity_default_request_mb", 4096)
    monkeypatch.setattr(settings, "daytona_capacity_total_memory_mb", 100_000)
    monkeypatch.setattr(settings, "daytona_capacity_headroom_memory_mb", 0)
    assert (
        trial_handler._capacity_request_memory_mb(
            _resources(request=8192, limit=None),
            (
                _resources(request=2048, limit=None),
                _resources(request=16_384, limit=None),
            ),
        )
        == 24_576
    )
    assert (
        trial_handler._capacity_request_memory_mb(
            _resources(request=None, limit=None), None
        )
        == 8192
    )


@pytest.mark.asyncio
async def test_waiter_heartbeats_then_acquires(monkeypatch):
    snapshots = [
        (
            False,
            provider_capacity.ProviderCapacitySnapshot(8, 10, 1, 2, 1, 4),
        ),
        (
            True,
            provider_capacity.ProviderCapacitySnapshot(10, 10, 2, 2, 0, 0),
        ),
    ]
    heartbeats = 0

    async def fake_try(**kwargs):
        return snapshots.pop(0)

    async def fake_wait_heartbeat():
        nonlocal heartbeats
        heartbeats += 1

    async def no_sleep(_seconds):
        return None

    async def no_lease_heartbeat(self):
        await self._stop.wait()

    monkeypatch.setattr(provider_capacity, "_try_acquire", fake_try)
    monkeypatch.setattr(provider_capacity.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(
        provider_capacity.ProviderCapacityLease,
        "_heartbeat_loop",
        no_lease_heartbeat,
    )
    lease = await provider_capacity.wait_for_provider_capacity(
        provider="daytona",
        owner_id="job:1:worker",
        requested_memory_mb=2,
        memory_limit_mb=10,
        lease_limit=2,
        lease_seconds=300,
        heartbeat_seconds=30,
        poll_seconds=0.01,
        wait_heartbeat=fake_wait_heartbeat,
    )
    assert heartbeats == 1
    lease._released = True
    lease._stop.set()
    await asyncio.gather(lease._heartbeat_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_waiter_failure_removes_unacquired_row(monkeypatch):
    async def fake_try(**kwargs):
        snapshot = provider_capacity.ProviderCapacitySnapshot(10, 10, 1, 1, 1, 1)
        return False, snapshot

    async def fail_heartbeat():
        raise RuntimeError("heartbeat failed")

    released: list[tuple[str, str]] = []

    async def fake_release(*, provider, owner_id):
        released.append((provider, owner_id))

    monkeypatch.setattr(provider_capacity, "_try_acquire", fake_try)
    monkeypatch.setattr(provider_capacity, "_release", fake_release)
    with pytest.raises(RuntimeError, match="heartbeat failed"):
        await provider_capacity.wait_for_provider_capacity(
            provider="daytona",
            owner_id="job:1:worker",
            requested_memory_mb=1,
            memory_limit_mb=10,
            lease_limit=1,
            lease_seconds=300,
            heartbeat_seconds=30,
            poll_seconds=0.01,
            wait_heartbeat=fail_heartbeat,
        )
    assert released == [("daytona", "job:1:worker")]


@pytest.mark.asyncio
async def test_trial_attempt_path_starts_only_after_capacity_acquisition(
    monkeypatch, tmp_path: Path
):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    plan = trial_handler.DaytonaCapacityPreflight(
        task_path_to_run=task_dir,
        temp_task_dir=None,
        resolved_task_s3_key=None,
        requested_memory_mb=4096,
    )
    sequence: list[str] = []

    class Lease:
        async def release(self):
            sequence.append("release")

    lease = Lease()

    async def fake_preflight(_trial_id):
        sequence.append("preflight")
        return plan

    async def fake_wait(**kwargs):
        sequence.append("acquire")
        return lease

    async def fake_run(*args, **kwargs):
        sequence.append("attempt")
        assert kwargs["capacity_lease"] is lease
        assert kwargs["resolved_task"] == (task_dir, None, None)

    monkeypatch.setattr(trial_handler, "_daytona_capacity_preflight", fake_preflight)
    monkeypatch.setattr(trial_handler, "wait_for_provider_capacity", fake_wait)
    monkeypatch.setattr(trial_handler, "_run_trial_job_with_capacity", fake_run)
    await trial_handler.run_trial_job(
        "trial-1",
        "openai/model-a",
        worker_id="worker-1",
        worker_job_id="job-1",
        worker_job_attempt=2,
    )
    assert sequence == ["preflight", "acquire", "attempt", "release"]


@pytest.mark.asyncio
async def test_skipped_trial_is_not_restarted_after_capacity_wait(monkeypatch):
    trial = SimpleNamespace(
        status=TrialStatus.SKIPPED,
        idempotency_key=None,
        agent="claude-code",
    )

    @asynccontextmanager
    async def fake_trial_session(_trial_id):
        yield SimpleNamespace(), trial

    async def unexpected_prepare(**kwargs):
        raise AssertionError("terminal trial must not start a new attempt")

    monkeypatch.setattr(trial_handler, "_trial_session", fake_trial_session)
    monkeypatch.setattr(trial_handler, "_prepare_trial_run", unexpected_prepare)
    await trial_handler._run_trial_job_with_capacity("trial-1", "openai/model-a")


@pytest.mark.parametrize(
    "terminal_status",
    (TrialStatus.SUCCESS, TrialStatus.FAILED, TrialStatus.SKIPPED),
)
@pytest.mark.asyncio
async def test_prepare_does_not_revive_trial_that_became_terminal(
    monkeypatch, terminal_status
):
    trial = SimpleNamespace(
        status=terminal_status,
        superseded_by_trial_id=None,
    )

    @asynccontextmanager
    async def fake_trial_session(
        _trial_id, *, allow_missing=False, with_for_update=False
    ):
        assert with_for_update is True
        yield SimpleNamespace(), trial

    monkeypatch.setattr(trial_handler, "_trial_session", fake_trial_session)

    prepared = await trial_handler._prepare_trial_run(
        trial_id="trial-1",
        worker_id="worker-1",
        queue_slot=1,
        modal_function_call_id="fc-1",
    )

    assert prepared is None
    assert trial.status == terminal_status


@pytest.mark.asyncio
async def test_capacity_wait_stops_when_worker_job_loses_ownership(
    monkeypatch, tmp_path: Path
):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    plan = trial_handler.DaytonaCapacityPreflight(
        task_path_to_run=task_dir,
        temp_task_dir=None,
        resolved_task_s3_key=None,
        requested_memory_mb=4096,
    )

    async def fake_preflight(_trial_id):
        return plan

    async def fake_wait(**kwargs):
        await kwargs["wait_heartbeat"]()
        raise AssertionError("ownership loss should interrupt the wait callback")

    async def lost_heartbeat(*args, **kwargs):
        return False

    monkeypatch.setattr(trial_handler, "_daytona_capacity_preflight", fake_preflight)
    monkeypatch.setattr(trial_handler, "wait_for_provider_capacity", fake_wait)
    monkeypatch.setattr(trial_handler, "heartbeat_worker_job", lost_heartbeat)
    with pytest.raises(RuntimeError, match="lost ownership"):
        await trial_handler.run_trial_job(
            "trial-1",
            "openai/model-a",
            worker_id="worker-1",
            worker_job_id="job-1",
            worker_job_attempt=2,
        )


@pytest.mark.asyncio
async def test_worker_job_heartbeat_reports_ownership(monkeypatch):
    class Connection:
        command = "UPDATE 0"

        async def execute(self, *args, **kwargs):
            return self.command

        async def close(self):
            return None

    connection = Connection()

    async def open_connection():
        return connection

    monkeypatch.setattr(worker_job_single_job, "_open_connection", open_connection)
    assert not await worker_job_single_job.heartbeat_worker_job(
        "job-1", current_worker_id="worker-1"
    )
    connection.command = "UPDATE 1"
    assert await worker_job_single_job.heartbeat_worker_job(
        "job-1", current_worker_id="worker-1"
    )


@pytest.mark.asyncio
async def test_capacity_lease_release_is_idempotent(monkeypatch):
    released: list[str] = []

    async def fake_release(*, provider, owner_id):
        released.append(owner_id)

    monkeypatch.setattr(provider_capacity, "_release", fake_release)
    lease = provider_capacity.ProviderCapacityLease(
        provider="daytona",
        owner_id="owner",
        requested_memory_mb=4096,
        lease_seconds=300,
        heartbeat_seconds=30,
    )
    await lease.release()
    await lease.release()
    assert released == ["owner"]


@pytest.mark.asyncio
async def test_capacity_lease_release_retries_after_database_failure(monkeypatch):
    attempts = 0

    async def flaky_release(*, provider, owner_id):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(provider_capacity, "_release", flaky_release)
    lease = provider_capacity.ProviderCapacityLease(
        provider="daytona",
        owner_id="owner",
        requested_memory_mb=4096,
        lease_seconds=300,
        heartbeat_seconds=30,
    )
    await lease.release()
    assert lease._released is False
    await lease.release()
    assert lease._released is True
    assert attempts == 2

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


def _source(
    *,
    task_version_id: str | None = "task-v1",
    task_s3_key: str | None = "tasks/task/v1.tar.gz",
    harbor_config_json: str | None = "{}",
    environment: str = "daytona",
) -> trial_handler.DaytonaCapacitySourceIdentity:
    return trial_handler.DaytonaCapacitySourceIdentity(
        task_id="task",
        task_version_id=task_version_id,
        task_path="task",
        task_s3_key=task_s3_key,
        task_version_content_hash="sha256:v1",
        environment=environment,
        harbor_config_json=harbor_config_json,
    )


def _patch_locked_trial(
    monkeypatch,
    trial,
    *,
    source: trial_handler.DaytonaCapacitySourceIdentity | None = None,
    task=None,
    session=None,
):
    session = session or SimpleNamespace()

    @asynccontextmanager
    async def fake_get_session():
        yield session

    async def fake_lock(observed_session, _trial_id, *, allow_missing: bool = False):
        assert observed_session is session
        return trial, source or _source(), task

    monkeypatch.setattr(trial_handler, "get_session", fake_get_session)
    monkeypatch.setattr(trial_handler, "_lock_capacity_source_then_trial", fake_lock)


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


def test_capacity_request_is_not_clamped_to_pool_limit(monkeypatch):
    monkeypatch.setattr(settings, "daytona_capacity_default_request_mb", 4096)
    monkeypatch.setattr(settings, "daytona_capacity_total_memory_mb", 10_000)
    monkeypatch.setattr(settings, "daytona_capacity_headroom_memory_mb", 0)
    assert (
        trial_handler._capacity_request_memory_mb(
            _resources(request=8192, limit=None),
            (_resources(request=4096, limit=None),),
        )
        == 12_288
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
async def test_oversized_waiter_is_rejected_before_database_access(monkeypatch):
    async def unexpected_try(**kwargs):
        raise AssertionError("oversized request must not create a waiter row")

    async def unexpected_release(**kwargs):
        raise AssertionError("no waiter row exists to release")

    monkeypatch.setattr(provider_capacity, "_try_acquire", unexpected_try)
    monkeypatch.setattr(provider_capacity, "_release", unexpected_release)

    with pytest.raises(
        provider_capacity.ProviderCapacityRequestTooLarge,
        match="12 MiB exceeds provider capacity limit 10 MiB",
    ):
        await provider_capacity.wait_for_provider_capacity(
            provider="daytona",
            owner_id="job:1:worker",
            requested_memory_mb=12,
            memory_limit_mb=10,
            lease_limit=1,
            lease_seconds=300,
            heartbeat_seconds=30,
            poll_seconds=0.01,
        )


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
        source_identity=_source(),
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
async def test_changed_preflight_releases_and_reaccounts_before_running(
    monkeypatch, tmp_path: Path
):
    old_temp = tmp_path / "old-download"
    old_temp.mkdir()
    new_temp = tmp_path / "new-download"
    new_temp.mkdir()
    old_source = _source()
    new_source = _source(
        task_version_id="task-v2",
        task_s3_key="tasks/task/v2.tar.gz",
    )
    plans = [
        trial_handler.DaytonaCapacityPreflight(
            task_path_to_run=old_temp,
            temp_task_dir=old_temp,
            resolved_task_s3_key=old_source.task_s3_key,
            requested_memory_mb=4096,
            source_identity=old_source,
        ),
        trial_handler.DaytonaCapacityPreflight(
            task_path_to_run=new_temp,
            temp_task_dir=new_temp,
            resolved_task_s3_key=new_source.task_s3_key,
            requested_memory_mb=8192,
            source_identity=new_source,
        ),
    ]
    requested: list[int] = []
    released: list[int] = []
    attempts: list[trial_handler.DaytonaCapacitySourceIdentity] = []

    class Lease:
        def __init__(self, requested_memory_mb: int):
            self.requested_memory_mb = requested_memory_mb

        async def release(self):
            released.append(self.requested_memory_mb)
            return True

    async def fake_preflight(_trial_id):
        return plans.pop(0)

    async def fake_wait(**kwargs):
        requested.append(kwargs["requested_memory_mb"])
        return Lease(kwargs["requested_memory_mb"])

    async def fake_run(*args, **kwargs):
        source = kwargs["expected_capacity_source"]
        attempts.append(source)
        if source == old_source:
            raise trial_handler.DaytonaCapacityPreflightChanged("source changed")

    monkeypatch.setattr(trial_handler, "_daytona_capacity_preflight", fake_preflight)
    monkeypatch.setattr(trial_handler, "wait_for_provider_capacity", fake_wait)
    monkeypatch.setattr(trial_handler, "_run_trial_job_with_capacity", fake_run)

    await trial_handler.run_trial_job("trial-1", "openai/model-a")

    assert requested == [4096, 8192]
    assert released == [4096, 8192]
    assert attempts == [old_source, new_source]
    assert not old_temp.exists()
    assert not new_temp.exists()


@pytest.mark.asyncio
async def test_non_daytona_snapshot_that_flips_to_daytona_reenters_capacity_gate(
    monkeypatch, tmp_path: Path
):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    plan = trial_handler.DaytonaCapacityPreflight(
        task_path_to_run=task_dir,
        temp_task_dir=None,
        resolved_task_s3_key=None,
        requested_memory_mb=4096,
        source_identity=_source(),
    )
    preflights = [None, plan]
    sequence: list[str] = []

    class Lease:
        async def release(self):
            sequence.append("release")
            return True

    async def fake_preflight(_trial_id):
        value = preflights.pop(0)
        sequence.append("preflight-none" if value is None else "preflight-daytona")
        return value

    async def flipped_source(_trial_id):
        sequence.append("routing-recheck")
        return _source()

    async def fake_wait(**kwargs):
        sequence.append("acquire")
        return Lease()

    async def fake_run(*args, **kwargs):
        sequence.append("run")

    monkeypatch.setattr(settings, "daytona_capacity_enabled", True)
    monkeypatch.setattr(trial_handler, "_daytona_capacity_preflight", fake_preflight)
    monkeypatch.setattr(
        trial_handler, "_active_daytona_capacity_source", flipped_source
    )
    monkeypatch.setattr(trial_handler, "wait_for_provider_capacity", fake_wait)
    monkeypatch.setattr(trial_handler, "_run_trial_job_with_capacity", fake_run)

    await trial_handler.run_trial_job("trial-1", "openai/model-a")

    assert sequence == [
        "preflight-none",
        "routing-recheck",
        "preflight-daytona",
        "acquire",
        "run",
        "release",
    ]


@pytest.mark.asyncio
async def test_oversized_preflight_is_failed_permanently_without_waiting(
    monkeypatch, tmp_path: Path
):
    temp_dir = tmp_path / "oversized-download"
    temp_dir.mkdir()
    plan = trial_handler.DaytonaCapacityPreflight(
        task_path_to_run=temp_dir,
        temp_task_dir=temp_dir,
        resolved_task_s3_key="tasks/task/v1.tar.gz",
        requested_memory_mb=12_288,
        source_identity=_source(),
    )
    rejected: list[tuple[int, int]] = []

    async def fake_preflight(_trial_id):
        return plan

    async def fake_reject(
        _trial_id,
        *,
        requested_memory_mb: int,
        memory_limit_mb: int,
        worker_id,
        worker_job_id,
        worker_job_attempt,
        expected_capacity_source,
    ):
        assert (worker_id, worker_job_id, worker_job_attempt) == (None, None, None)
        assert expected_capacity_source == plan.source_identity
        rejected.append((requested_memory_mb, memory_limit_mb))

    async def unexpected_wait(**kwargs):
        raise AssertionError("oversized request must not enter the FIFO")

    monkeypatch.setattr(settings, "daytona_capacity_total_memory_mb", 10_000)
    monkeypatch.setattr(settings, "daytona_capacity_headroom_memory_mb", 0)
    monkeypatch.setattr(trial_handler, "_daytona_capacity_preflight", fake_preflight)
    monkeypatch.setattr(trial_handler, "_reject_daytona_capacity_request", fake_reject)
    monkeypatch.setattr(trial_handler, "wait_for_provider_capacity", unexpected_wait)

    await trial_handler.run_trial_job("trial-1", "openai/model-a")

    assert rejected == [(12_288, 10_000)]
    assert not temp_dir.exists()


@pytest.mark.asyncio
async def test_changed_oversized_preflight_reaccounts_instead_of_rejecting(
    monkeypatch, tmp_path: Path
):
    old_temp = tmp_path / "oversized-old"
    old_temp.mkdir()
    new_temp = tmp_path / "fitting-new"
    new_temp.mkdir()
    old_source = _source()
    new_source = _source(
        task_version_id="task-v2",
        task_s3_key="tasks/task/v2.tar.gz",
    )
    plans = [
        trial_handler.DaytonaCapacityPreflight(
            task_path_to_run=old_temp,
            temp_task_dir=old_temp,
            resolved_task_s3_key=old_source.task_s3_key,
            requested_memory_mb=12_288,
            source_identity=old_source,
        ),
        trial_handler.DaytonaCapacityPreflight(
            task_path_to_run=new_temp,
            temp_task_dir=new_temp,
            resolved_task_s3_key=new_source.task_s3_key,
            requested_memory_mb=4096,
            source_identity=new_source,
        ),
    ]
    rejected_sources: list[trial_handler.DaytonaCapacitySourceIdentity] = []
    acquired: list[int] = []
    ran: list[trial_handler.DaytonaCapacitySourceIdentity] = []

    class Lease:
        async def release(self):
            return True

    async def fake_preflight(_trial_id):
        return plans.pop(0)

    async def stale_reject(
        _trial_id,
        *,
        requested_memory_mb: int,
        memory_limit_mb: int,
        worker_id,
        worker_job_id,
        worker_job_attempt,
        expected_capacity_source,
    ):
        assert (worker_id, worker_job_id, worker_job_attempt) == (None, None, None)
        assert requested_memory_mb == 12_288
        assert memory_limit_mb == 10_000
        rejected_sources.append(expected_capacity_source)
        raise trial_handler.DaytonaCapacityPreflightChanged("source changed")

    async def fake_wait(**kwargs):
        acquired.append(kwargs["requested_memory_mb"])
        return Lease()

    async def fake_run(*args, **kwargs):
        ran.append(kwargs["expected_capacity_source"])

    monkeypatch.setattr(settings, "daytona_capacity_total_memory_mb", 10_000)
    monkeypatch.setattr(settings, "daytona_capacity_headroom_memory_mb", 0)
    monkeypatch.setattr(trial_handler, "_daytona_capacity_preflight", fake_preflight)
    monkeypatch.setattr(trial_handler, "_reject_daytona_capacity_request", stale_reject)
    monkeypatch.setattr(trial_handler, "wait_for_provider_capacity", fake_wait)
    monkeypatch.setattr(trial_handler, "_run_trial_job_with_capacity", fake_run)

    await trial_handler.run_trial_job("trial-1", "openai/model-a")

    assert rejected_sources == [old_source]
    assert acquired == [4096]
    assert ran == [new_source]
    assert not old_temp.exists()
    assert not new_temp.exists()


@pytest.mark.asyncio
async def test_capacity_rejection_marks_trial_terminal_and_runs_completion_hooks(
    monkeypatch,
):
    trial = SimpleNamespace(
        status=TrialStatus.QUEUED,
        superseded_by_trial_id=None,
        harbor_stage=None,
        error_message=None,
        finished_at=None,
        next_retry_at="later",
        current_worker_id="worker-1",
        current_queue_slot=1,
        heartbeat_at=None,
        org_id="org-1",
        billed_user_id="user-1",
    )
    settled: list[dict] = []

    async def fake_finish(**kwargs):
        settled.append(kwargs)

    _patch_locked_trial(monkeypatch, trial)
    monkeypatch.setattr(trial_handler, "_finish_trial_settlement", fake_finish)

    await trial_handler._reject_daytona_capacity_request(
        "trial-1", requested_memory_mb=12_288, memory_limit_mb=10_000
    )

    assert trial.status == TrialStatus.FAILED
    assert trial.harbor_stage == "capacity_rejected"
    assert "12288 MiB" in trial.error_message
    assert trial.finished_at is not None
    assert trial.next_retry_at is None
    assert trial.current_worker_id is None
    assert trial.current_queue_slot is None
    assert settled == [
        {
            "trial_id": "trial-1",
            "org_id": "org-1",
            "billed_user_id": "user-1",
            "run_post_trial_hooks": True,
        }
    ]


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

    _patch_locked_trial(monkeypatch, trial)

    prepared = await trial_handler._prepare_trial_run(
        trial_id="trial-1",
        worker_id="worker-1",
        queue_slot=1,
        modal_function_call_id="fc-1",
    )

    assert prepared is None
    assert trial.status == terminal_status


@pytest.mark.asyncio
async def test_prepare_rejects_changed_capacity_source_before_mutating_trial(
    monkeypatch,
):
    trial = SimpleNamespace(
        id="trial-1",
        status=TrialStatus.QUEUED,
        superseded_by_trial_id=None,
        task_id="task",
        task_version_id="task-v2",
        environment="daytona",
        harbor_config={},
    )
    current_source = _source(
        task_version_id="task-v2",
        task_s3_key="tasks/task/v2.tar.gz",
    )
    _patch_locked_trial(monkeypatch, trial, source=current_source)

    with pytest.raises(
        trial_handler.DaytonaCapacityPreflightChanged,
        match="inputs changed",
    ):
        await trial_handler._prepare_trial_run(
            trial_id="trial-1",
            worker_id="worker-1",
            queue_slot=1,
            modal_function_call_id="fc-1",
            expected_capacity_source=_source(),
        )

    assert trial.status == TrialStatus.QUEUED


@pytest.mark.asyncio
async def test_capacity_mutation_locks_task_and_version_before_trial():
    trial = SimpleNamespace(
        task_id="task",
        task_version_id="task-v1",
        environment="daytona",
        harbor_config={},
    )
    task = SimpleNamespace(
        id="task",
        task_path="task",
        task_s3_key="tasks/task/v1.tar.gz",
    )
    version = SimpleNamespace(
        task_path="task",
        task_s3_key="tasks/task/v1.tar.gz",
        content_hash="sha256:v1",
    )
    calls: list[tuple[object, dict]] = []

    class Session:
        async def get(self, model, _object_id, **kwargs):
            calls.append((model, kwargs))
            if model is trial_handler.TaskModel:
                return task
            if model is trial_handler.TaskVersionModel:
                return version
            if model is trial_handler.TrialModel:
                return trial
            raise AssertionError(f"unexpected model {model}")

    (
        locked_trial,
        source,
        locked_task,
    ) = await trial_handler._lock_capacity_source_then_trial(Session(), "trial-1")

    assert calls == [
        (trial_handler.TrialModel, {}),
        (trial_handler.TaskModel, {"with_for_update": True}),
        (trial_handler.TaskVersionModel, {"with_for_update": True}),
        (
            trial_handler.TrialModel,
            {"with_for_update": True, "populate_existing": True},
        ),
    ]
    assert locked_trial is trial
    assert locked_task is task
    assert source == _source()


@pytest.mark.asyncio
async def test_prepare_fences_worker_job_ownership_before_starting_attempt(monkeypatch):
    trial = SimpleNamespace(
        status=TrialStatus.QUEUED,
        superseded_by_trial_id=None,
        attempts=0,
    )
    worker_job = SimpleNamespace(
        status=trial_handler.WorkerJobStatus.RETRYING,
        current_worker_id=None,
        attempts=2,
        subject_id="trial-1",
    )

    class Session:
        async def get(self, model, object_id, **kwargs):
            assert model is trial_handler.WorkerJobModel
            assert object_id == "job-1"
            assert kwargs == {"with_for_update": True}
            return worker_job

    _patch_locked_trial(monkeypatch, trial, session=Session())

    with pytest.raises(
        trial_handler.TrialWorkerJobOwnershipLost,
        match="lost ownership before trial",
    ):
        await trial_handler._prepare_trial_run(
            trial_id="trial-1",
            worker_id="worker-1",
            queue_slot=1,
            modal_function_call_id="fc-1",
            worker_job_id="job-1",
            worker_job_attempt=2,
        )

    assert trial.status == TrialStatus.QUEUED
    assert trial.attempts == 0


@pytest.mark.asyncio
async def test_capacity_rejection_fences_worker_job_ownership(monkeypatch):
    trial = SimpleNamespace(
        status=TrialStatus.QUEUED,
        superseded_by_trial_id=None,
        harbor_stage=None,
        error_message=None,
        finished_at=None,
    )
    worker_job = SimpleNamespace(
        status=trial_handler.WorkerJobStatus.RETRYING,
        current_worker_id=None,
        attempts=2,
        subject_id="trial-1",
    )

    class Session:
        async def get(self, model, object_id, **kwargs):
            assert model is trial_handler.WorkerJobModel
            assert object_id == "job-1"
            assert kwargs == {"with_for_update": True}
            return worker_job

    async def unexpected_finish(**kwargs):
        raise AssertionError("obsolete worker must not settle a rejected trial")

    _patch_locked_trial(monkeypatch, trial, session=Session())
    monkeypatch.setattr(trial_handler, "_finish_trial_settlement", unexpected_finish)

    with pytest.raises(trial_handler.TrialWorkerJobOwnershipLost):
        await trial_handler._reject_daytona_capacity_request(
            "trial-1",
            requested_memory_mb=12_288,
            memory_limit_mb=10_000,
            worker_id="worker-1",
            worker_job_id="job-1",
            worker_job_attempt=2,
        )

    assert trial.status == TrialStatus.QUEUED
    assert trial.harbor_stage is None
    assert trial.error_message is None
    assert trial.finished_at is None


@pytest.mark.asyncio
async def test_ownership_loss_after_acquire_releases_without_running(
    monkeypatch, tmp_path: Path
):
    temp_dir = tmp_path / "ownership-lost-download"
    temp_dir.mkdir()
    plan = trial_handler.DaytonaCapacityPreflight(
        task_path_to_run=temp_dir,
        temp_task_dir=temp_dir,
        resolved_task_s3_key=None,
        requested_memory_mb=4096,
        source_identity=_source(),
    )
    released = 0

    class Lease:
        async def release(self):
            nonlocal released
            released += 1
            return True

    async def fake_preflight(_trial_id):
        return plan

    async def fake_wait(**kwargs):
        return Lease()

    async def ownership_lost(*args, **kwargs):
        raise trial_handler.TrialWorkerJobOwnershipLost("claim was reaped")

    monkeypatch.setattr(trial_handler, "_daytona_capacity_preflight", fake_preflight)
    monkeypatch.setattr(trial_handler, "wait_for_provider_capacity", fake_wait)
    monkeypatch.setattr(trial_handler, "_run_trial_job_with_capacity", ownership_lost)

    await trial_handler.run_trial_job(
        "trial-1",
        "openai/model-a",
        worker_id="worker-1",
        worker_job_id="job-1",
        worker_job_attempt=2,
    )

    assert released == 1
    assert not temp_dir.exists()


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
        source_identity=_source(),
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
async def test_capacity_wait_drops_terminal_trial_before_acquiring(
    monkeypatch, tmp_path: Path
):
    temp_dir = tmp_path / "terminal-wait-download"
    temp_dir.mkdir()
    plan = trial_handler.DaytonaCapacityPreflight(
        task_path_to_run=temp_dir,
        temp_task_dir=temp_dir,
        resolved_task_s3_key=None,
        requested_memory_mb=4096,
        source_identity=_source(),
    )
    checks = 0

    async def fake_preflight(_trial_id):
        return plan

    async def inactive(_trial_id):
        return False

    async def fake_wait(**kwargs):
        nonlocal checks
        checks += 1
        await kwargs["wait_check"]()
        raise AssertionError("obsolete wait check must stop before acquisition")

    async def unexpected_run(*args, **kwargs):
        raise AssertionError("terminal trial must not execute")

    monkeypatch.setattr(trial_handler, "_daytona_capacity_preflight", fake_preflight)
    monkeypatch.setattr(trial_handler, "_daytona_capacity_wait_is_active", inactive)
    monkeypatch.setattr(trial_handler, "wait_for_provider_capacity", fake_wait)
    monkeypatch.setattr(trial_handler, "_run_trial_job_with_capacity", unexpected_run)

    await trial_handler.run_trial_job("trial-1", "openai/model-a")

    assert checks == 1
    assert not temp_dir.exists()


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

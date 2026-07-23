from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from harbor.trial.hooks import TrialEvent

import oddish.costs.recorder as recorder
from oddish.costs.modal_cost import SpanResources
from oddish.workers.harbor.runner import (
    capture_live_sandbox_resources,
    capture_sandbox_resources,
    capture_verifier_resources,
)
from oddish.workers.queue import trial_handler
from oddish.workers.queue.trial_handler import SandboxCostState
from oddish.workers.queue import worker_job_single_job
from oddish.workers.queue.worker_job_single_job import ClaimedWorkerJob
from oddish.db import TrialStatus, WorkerJobKind
from oddish.workers.jobs.registry import JobOutcome


def _fallback() -> SpanResources:
    return SpanResources(
        cpu_request=None,
        cpu_limit=None,
        mem_request_mb=None,
        mem_limit_mb=None,
        gpu_type=None,
        gpu_count=0,
        price_multiplier=Decimal(1),
        container_class="sandbox",
        spec_source="unknown",
    )


def test_prefork_resource_capture_models_modal_auto_and_overrides(make_task) -> None:
    task = make_task(
        task_toml="""\
version = "1.0"
[agent]
timeout_sec = 300
[verifier]
timeout_sec = 120
[environment]
cpus = 8
memory_mb = 4096
gpus = 2
gpu_types = ["a10g"]
"""
    )
    automatic = capture_sandbox_resources(task, None)
    assert automatic.cpu_request == 8
    assert automatic.cpu_limit == 8
    assert automatic.mem_request_mb == 4096
    assert automatic.mem_limit_mb is None
    assert automatic.gpu_type == "A10"
    assert automatic.cpu_enforcement_mode == "auto"
    assert automatic.mem_enforcement_mode == "auto"
    assert automatic.spec_source == "pinned"

    overridden = capture_sandbox_resources(
        task,
        {
            "environment": {
                "override_cpus": 4,
                "override_memory_mb": 2048,
                "override_gpus": 1,
                "cpu_enforcement_policy": "limit",
                "memory_enforcement_policy": "limit",
            }
        },
    )
    assert overridden.cpu_request == 0.125
    assert overridden.cpu_limit == 4
    assert overridden.mem_request_mb == 128
    assert overridden.mem_limit_mb == 2048
    assert overridden.gpu_count == 1
    assert overridden.spec_source == "override"


def test_separate_verifier_capture_only_for_separate_environment(make_task) -> None:
    shared = make_task(name="shared")
    assert capture_verifier_resources(shared, None) is None

    separate = make_task(
        name="separate",
        task_toml="""\
version = "1.0"
[agent]
timeout_sec = 300
[environment]
cpus = 1
memory_mb = 1024
[verifier]
timeout_sec = 120
environment_mode = "separate"
[verifier.environment]
cpus = 3
memory_mb = 6144
""",
    )
    resources = capture_verifier_resources(separate, None)
    assert resources is not None
    assert resources.cpu_request == 3
    assert resources.cpu_limit == 3
    assert resources.mem_request_mb == 6144


def test_live_environment_resources_override_prefork_snapshot() -> None:
    env = SimpleNamespace(
        task_env_config=SimpleNamespace(
            cpus=7, memory_mb=9000, gpus=1, gpu_types=["h100"]
        ),
        _cpu_resource_mode="request",
        _memory_resource_mode="limit",
        _override_cpus=7,
        _override_memory_mb=None,
        _override_gpus=None,
        _cpu_config=lambda: 7,
        _memory_config=lambda: (128, 9000),
    )
    resources = capture_live_sandbox_resources(env, _fallback())
    assert resources.cpu_request == 7
    assert resources.cpu_limit is None
    assert resources.mem_request_mb == 128
    assert resources.mem_limit_mb == 9000
    assert resources.gpu_type == "H100"
    assert resources.spec_source == "override"
    assert resources.cpu_enforcement_mode == "request"
    assert resources.mem_enforcement_mode == "limit"


@pytest.mark.asyncio
async def test_settlement_reuses_hook_boundary_and_adds_verifier_span(
    monkeypatch,
) -> None:
    calls: list[tuple[str, dict]] = []

    async def close(*args, **kwargs):
        calls.append(("close", {"args": args, **kwargs}))

    async def verifier(**kwargs):
        calls.append(("verifier", kwargs))

    async def price(*args, **kwargs):
        calls.append(("price", {"args": args, **kwargs}))

    monkeypatch.setattr(trial_handler, "close_agent_sandboxes", close)
    monkeypatch.setattr(trial_handler, "record_verifier_span", verifier)
    monkeypatch.setattr(trial_handler, "price_unpriced_spans", price)

    terminal = datetime(2026, 7, 22, 1, 2, 3, tzinfo=timezone.utc)
    state = SandboxCostState(
        resources=_fallback(),
        verifier_resources=_fallback(),
        provider="modal",
        trial_id="trial-1",
        attempt=2,
        experiment_id="exp-1",
        org_id="org-1",
        billed_user_id="user-1",
        worker_job_id="job-1",
        worker_job_attempt=3,
        terminal_at=terminal,
    )
    outcome = SimpleNamespace(
        phase_timing={
            "verifier": {
                "started_at": "2026-07-22T01:01:00+00:00",
                "finished_at": "2026-07-22T01:02:00+00:00",
            }
        }
    )

    await trial_handler._settle_compute_costs(state, outcome)

    assert [name for name, _ in calls] == ["close", "verifier", "price"]
    assert calls[0][1]["finished_at"] == terminal
    assert calls[1][1]["worker_job_attempt"] == 3
    assert calls[1][1]["started_at"] == datetime(2026, 7, 22, 1, 1, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_settlement_failure_never_escapes(monkeypatch) -> None:
    async def broken(*args, **kwargs):
        raise RuntimeError("accounting unavailable")

    monkeypatch.setattr(trial_handler, "close_agent_sandboxes", broken)
    state = SandboxCostState(
        resources=_fallback(),
        verifier_resources=None,
        provider="modal",
        trial_id="trial-1",
        attempt=1,
        experiment_id=None,
        org_id=None,
        billed_user_id=None,
        worker_job_id="job-1",
        worker_job_attempt=1,
        terminal_at=datetime.now(timezone.utc),
    )
    await trial_handler._settle_compute_costs(state, None)


@pytest.mark.asyncio
async def test_terminal_hook_closes_cost_span_before_ownership_guard(
    monkeypatch,
) -> None:
    terminal = datetime(2026, 7, 22, 4, 5, 6, tzinfo=timezone.utc)
    closed: list[dict] = []

    async def close(*args, **kwargs):
        closed.append({"args": args, **kwargs})

    @asynccontextmanager
    async def trial_session(*args, **kwargs):
        trial = SimpleNamespace(
            superseded_by_trial_id="replacement",
            attempts=2,
            status=TrialStatus.RETRYING,
        )
        yield SimpleNamespace(), trial

    monkeypatch.setattr(trial_handler, "close_agent_sandboxes", close)
    monkeypatch.setattr(trial_handler, "_trial_session", trial_session)
    state = SandboxCostState(
        resources=_fallback(),
        verifier_resources=None,
        provider="modal",
        trial_id="trial-1",
        attempt=2,
        experiment_id=None,
        org_id=None,
        billed_user_id=None,
        worker_job_id="job-1",
        worker_job_attempt=3,
    )
    event = SimpleNamespace(
        event=TrialEvent.END,
        timestamp=terminal,
        environment=None,
        environment_provider="modal",
        environment_external_id="sb-1",
        result=None,
    )

    await trial_handler._handle_harbor_event(
        event,
        trial_id="trial-1",
        worker_job_id="job-1",
        worker_job_attempt=3,
        cost_state=state,
    )

    assert state.terminal_at == terminal
    assert closed == [{"args": ("job-1", 3), "finished_at": terminal}]


@pytest.mark.asyncio
async def test_worker_span_uses_claim_and_outcome_boundaries(monkeypatch) -> None:
    claimed_at = datetime(2026, 7, 22, 5, tzinfo=timezone.utc)
    job = ClaimedWorkerJob(
        id="job-1",
        kind=WorkerJobKind.QA_REVIEW,
        queue_key="qa",
        subject_table=None,
        subject_id=None,
        payload={},
        attempts=2,
        max_attempts=3,
        org_id=None,
        parent_job_id=None,
        claimed_at=claimed_at,
    )
    calls: list[tuple[str, object]] = []

    async def claim(*args, **kwargs):
        return job

    class Handler:
        async def run(self, claimed):
            assert claimed is job
            return JobOutcome.ok()

    async def open_span(claimed, spec, *, started_at):
        calls.append(("open", started_at))

    async def record(**kwargs):
        return True

    async def close_span(*args, **kwargs):
        calls.append(("close", kwargs["finished_at"]))

    monkeypatch.setattr(
        worker_job_single_job, "_ensure_handlers_registered", lambda: None
    )
    monkeypatch.setattr(worker_job_single_job, "claim_single_worker_job", claim)
    monkeypatch.setattr(worker_job_single_job, "get_handler", lambda kind: Handler())
    monkeypatch.setattr(worker_job_single_job, "open_worker_span", open_span)
    monkeypatch.setattr(worker_job_single_job, "_record_outcome", record)
    monkeypatch.setattr(worker_job_single_job, "close_worker_span", close_span)

    await worker_job_single_job.run_single_worker_job(
        "qa",
        worker_id="worker-1",
        queue_slot=0,
        worker_billing_spec=recorder.WorkerBillingSpec(1, 3072, True),
    )

    assert calls[0] == ("open", claimed_at)
    assert calls[1][0] == "close"
    assert calls[1][1] >= claimed_at


def test_missing_table_warning_is_logged_once(caplog) -> None:
    class UndefinedTableError(Exception):
        sqlstate = "42P01"

    exc = recorder.ProgrammingError("select", {}, UndefinedTableError())
    recorder._missing_table_logged = False
    with caplog.at_level("WARNING"):
        recorder._record_failure("first", exc)
        recorder._record_failure("second", exc)
    assert (
        sum("tables are not available" in record.message for record in caplog.records)
        == 1
    )

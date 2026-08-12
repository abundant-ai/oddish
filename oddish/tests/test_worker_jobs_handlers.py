"""Tests for the per-kind JobHandler wrappers in ``oddish.workers.jobs.handlers``.

The handlers are thin adapters: they delegate to the existing
``run_trial_job`` / ``run_task_qa_job`` / ``run_analysis_job``
functions and then inspect the domain row's terminal state to decide
the ``JobOutcome`` that drives the ``worker_jobs`` row's transition.

These tests verify that glue layer without pulling in a real DB -- the
underlying ``run_*_job`` calls are stubbed, and the domain read is
mocked via a fake ``get_session``.
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.db import (  # noqa: E402
    AnalysisStatus,
    TaskStatus,
    TrialStatus,
    VerdictStatus,
    WorkerJobKind,
    WorkerJobStatus,
)
from oddish.queue import TaskQAStageAdmission  # noqa: E402
from oddish.workers.jobs import handlers as handlers_module  # noqa: E402
from oddish.workers.jobs.handlers import (  # noqa: E402
    AnalysisJobHandler,
    QaJobHandler,
    TrialJobHandler,
)
from oddish.workers.queue.worker_job_single_job import ClaimedWorkerJob  # noqa: E402


def _fake_get_session_factory(
    domain_row, *, worker_status=WorkerJobStatus.RUNNING, qa_owner_id="wj-vd-1"
):
    """Build a ``get_session``-compatible context manager for tests."""

    class _Session:
        async def get(self, model, obj_id, **_kwargs):
            if domain_row is None:
                return None
            if getattr(model, "__name__", None) == "TaskVersionModel":
                return SimpleNamespace(
                    content_hash=getattr(
                        domain_row, "current_version_content_hash", None
                    )
                )
            return domain_row

        async def scalar(self, _statement, _params=None):
            if "ORDER BY created_at" in str(_statement):
                return qa_owner_id
            return worker_status

    @asynccontextmanager
    async def _get_session():
        yield _Session()

    return _get_session


def _patch_run(monkeypatch, fn_name: str):
    """Install a no-op stub for the underlying ``run_*_job`` call."""
    called = {"args": None, "kwargs": None}

    async def _stub(*args, **kwargs):
        called["args"] = args
        called["kwargs"] = kwargs

    monkeypatch.setattr(handlers_module, fn_name, _stub)
    return called


def _trial_claim(**overrides) -> ClaimedWorkerJob:
    defaults = dict(
        id="wj-1",
        kind=WorkerJobKind.TRIAL,
        queue_key="openai/gpt-5",
        subject_table="trials",
        subject_id="trial-abc",
        payload={"trial_id": "trial-abc"},
        attempts=1,
        max_attempts=6,
        org_id=None,
        parent_job_id=None,
        worker_id="w-1",
        queue_slot=0,
        modal_function_call_id=None,
    )
    defaults.update(overrides)
    return ClaimedWorkerJob(**defaults)


def _analysis_claim(**overrides) -> ClaimedWorkerJob:
    defaults = dict(
        id="wj-an-1",
        kind=WorkerJobKind.ANALYSIS,
        queue_key="analysis",
        subject_table="trials",
        subject_id="trial-abc",
        payload={"trial_id": "trial-abc"},
        attempts=1,
        max_attempts=6,
        org_id=None,
        parent_job_id=None,
    )
    defaults.update(overrides)
    return ClaimedWorkerJob(**defaults)


def _verdict_claim(**overrides) -> ClaimedWorkerJob:
    defaults = dict(
        id="wj-vd-1",
        kind=WorkerJobKind.QA,
        queue_key="qa",
        subject_table="tasks",
        subject_id="task-xyz",
        payload={"task_id": "task-xyz"},
        attempts=1,
        max_attempts=6,
        org_id=None,
        parent_job_id=None,
    )
    defaults.update(overrides)
    return ClaimedWorkerJob(**defaults)


# ---------------------------------------------------------------------------
# TrialJobHandler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trial_handler_returns_ok_on_success(monkeypatch):
    trial_row = SimpleNamespace(
        status=TrialStatus.SUCCESS,
        error_message=None,
    )
    monkeypatch.setattr(
        handlers_module, "get_session", _fake_get_session_factory(trial_row)
    )
    called = _patch_run(monkeypatch, "run_trial_job")

    outcome = await TrialJobHandler().run(_trial_claim())

    assert outcome.success is not None
    assert outcome.failure is None
    assert called["kwargs"]["queue_key"] == "openai/gpt-5"
    assert called["kwargs"]["worker_id"] == "w-1"


@pytest.mark.asyncio
async def test_trial_handler_returns_retryable_fail_on_retrying(monkeypatch):
    trial_row = SimpleNamespace(
        status=TrialStatus.RETRYING,
        error_message="timeout on attempt 1",
    )
    monkeypatch.setattr(
        handlers_module, "get_session", _fake_get_session_factory(trial_row)
    )
    _patch_run(monkeypatch, "run_trial_job")

    outcome = await TrialJobHandler().run(_trial_claim())

    assert outcome.failure is not None
    assert outcome.failure.retryable is True
    assert "timeout" in outcome.failure.error_message


@pytest.mark.asyncio
async def test_trial_handler_returns_retryable_fail_on_failed_with_budget(monkeypatch):
    trial_row = SimpleNamespace(
        status=TrialStatus.FAILED,
        error_message="harbor crash",
    )
    monkeypatch.setattr(
        handlers_module, "get_session", _fake_get_session_factory(trial_row)
    )
    _patch_run(monkeypatch, "run_trial_job")

    outcome = await TrialJobHandler().run(_trial_claim(attempts=1, max_attempts=6))

    assert outcome.failure is not None
    # Handler delegates the budget decision to ``_record_outcome``
    # in the runner -- "retryable=True" means "retry if attempts
    # remain", not "always retry".
    assert outcome.failure.retryable is True


@pytest.mark.asyncio
async def test_trial_handler_returns_permanent_fail_on_modal_image_build(monkeypatch):
    trial_row = SimpleNamespace(
        status=TrialStatus.FAILED,
        harbor_stage="image_build_failed",
        error_message="Harbor job execution failed: RuntimeError: Image build for im-abc123 failed",
    )
    monkeypatch.setattr(
        handlers_module, "get_session", _fake_get_session_factory(trial_row)
    )
    _patch_run(monkeypatch, "run_trial_job")

    outcome = await TrialJobHandler().run(_trial_claim(attempts=1, max_attempts=6))

    assert outcome.failure is not None
    assert outcome.failure.retryable is False
    assert "Image build for im-abc123 failed" in outcome.failure.error_message


@pytest.mark.asyncio
async def test_trial_handler_fails_permanently_when_row_missing(monkeypatch):
    monkeypatch.setattr(handlers_module, "get_session", _fake_get_session_factory(None))
    _patch_run(monkeypatch, "run_trial_job")

    outcome = await TrialJobHandler().run(_trial_claim())

    assert outcome.failure is not None
    assert outcome.failure.retryable is False
    assert "vanished" in outcome.failure.error_message


@pytest.mark.asyncio
async def test_trial_handler_rejects_missing_subject_id(monkeypatch):
    _patch_run(monkeypatch, "run_trial_job")
    claim = _trial_claim(subject_id=None)
    with pytest.raises(ValueError, match="missing subject_id"):
        await TrialJobHandler().run(claim)


# ---------------------------------------------------------------------------
# AnalysisJobHandler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analysis_handler_returns_ok_on_success(monkeypatch):
    # Start with no analysis state; the stub fakes a successful run.
    trial_row = SimpleNamespace(
        analysis_status=AnalysisStatus.RUNNING,
        analysis_error=None,
    )
    monkeypatch.setattr(
        handlers_module, "get_session", _fake_get_session_factory(trial_row)
    )

    calls = {"queue_key": None}

    async def _stub_run(*args, **kwargs):
        calls["queue_key"] = kwargs.get("queue_key")
        trial_row.analysis_status = AnalysisStatus.SUCCESS

    monkeypatch.setattr(handlers_module, "run_analysis_job", _stub_run)

    outcome = await AnalysisJobHandler().run(_analysis_claim())

    assert outcome.success is not None
    assert calls["queue_key"] == "analysis"


@pytest.mark.asyncio
async def test_analysis_handler_resets_terminal_state_on_retry(monkeypatch):
    # When worker_jobs re-claims an analysis, the handler should reset
    # a stale SUCCESS/FAILED analysis_status back to QUEUED so the
    # underlying ``run_analysis_job`` idempotency guard doesn't
    # short-circuit.
    trial_row = SimpleNamespace(
        analysis_status=AnalysisStatus.FAILED,
        analysis_error="prior failure",
        analysis_finished_at="2026-04-20",
    )
    monkeypatch.setattr(
        handlers_module, "get_session", _fake_get_session_factory(trial_row)
    )

    pre_state_seen = {"status": None}

    async def _stub_run(*args, **kwargs):
        # Confirm by the time run_analysis_job fires the reset happened.
        pre_state_seen["status"] = trial_row.analysis_status
        trial_row.analysis_status = AnalysisStatus.SUCCESS
        trial_row.analysis_error = None

    monkeypatch.setattr(handlers_module, "run_analysis_job", _stub_run)

    outcome = await AnalysisJobHandler().run(_analysis_claim())

    assert pre_state_seen["status"] == AnalysisStatus.QUEUED
    assert outcome.success is not None


@pytest.mark.asyncio
async def test_analysis_handler_returns_retryable_fail_on_failed_status(monkeypatch):
    trial_row = SimpleNamespace(
        analysis_status=AnalysisStatus.FAILED,
        analysis_error="classifier 500",
    )

    # Call count lets us verify the reset path didn't accidentally swallow
    # the "retryable failure" signal.
    monkeypatch.setattr(
        handlers_module, "get_session", _fake_get_session_factory(trial_row)
    )

    async def _stub_run(*args, **kwargs):
        # Reset happened before this; leaving analysis_status=FAILED
        # after ``run_analysis_job`` simulates a real failure.
        trial_row.analysis_status = AnalysisStatus.FAILED

    monkeypatch.setattr(handlers_module, "run_analysis_job", _stub_run)

    outcome = await AnalysisJobHandler().run(_analysis_claim())

    assert outcome.failure is not None
    assert outcome.failure.retryable is True


# ---------------------------------------------------------------------------
# QaJobHandler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qa_handler_returns_ok_on_success(monkeypatch):
    task_row = SimpleNamespace(
        verdict_status=VerdictStatus.RUNNING,
        verdict_error=None,
    )
    monkeypatch.setattr(
        handlers_module, "get_session", _fake_get_session_factory(task_row)
    )

    async def _stub_run(*args, **kwargs):
        assert kwargs["task_version_id"] is None
        assert kwargs["enforce_task_version_id"] is False
        assert kwargs["task_version_content_hash"] is None
        assert kwargs["enforce_task_version_content_hash"] is False
        task_row.verdict_status = VerdictStatus.SUCCESS

    monkeypatch.setattr(handlers_module, "run_task_qa_job", _stub_run)

    outcome = await QaJobHandler().run(_verdict_claim())

    assert outcome.success is not None


@pytest.mark.asyncio
async def test_qa_handler_resets_terminal_state_on_retry(monkeypatch):
    task_row = SimpleNamespace(
        verdict_status=VerdictStatus.FAILED,
        verdict_error="previous crash",
        verdict_finished_at="2026-04-20",
    )
    monkeypatch.setattr(
        handlers_module, "get_session", _fake_get_session_factory(task_row)
    )

    state_at_run = {"status": None}

    async def _stub_run(*args, **kwargs):
        state_at_run["status"] = task_row.verdict_status
        task_row.verdict_status = VerdictStatus.SUCCESS
        task_row.verdict_error = None

    monkeypatch.setattr(handlers_module, "run_task_qa_job", _stub_run)

    outcome = await QaJobHandler().run(_verdict_claim())

    assert state_at_run["status"] == VerdictStatus.QUEUED
    assert outcome.success is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"task_id": "task-xyz"},
        {
            "task_id": "task-xyz",
            "task_version_id": "task-v2",
            "task_version_content_hash": "hash-v2",
        },
    ],
)
async def test_qa_handler_does_not_revive_completed_task(monkeypatch, payload):
    task_row = SimpleNamespace(
        status=TaskStatus.COMPLETED,
        verdict_status=VerdictStatus.SUCCESS,
        verdict_error=None,
    )
    monkeypatch.setattr(
        handlers_module, "get_session", _fake_get_session_factory(task_row)
    )

    async def fail_run(*_args, **_kwargs):
        pytest.fail("a terminal task's retired QA worker must not execute")

    monkeypatch.setattr(handlers_module, "run_task_qa_job", fail_run)

    outcome = await QaJobHandler().run(_verdict_claim(payload=payload))

    assert outcome.success is not None
    assert task_row.status == TaskStatus.COMPLETED
    assert task_row.verdict_status == VerdictStatus.SUCCESS


@pytest.mark.asyncio
async def test_qa_handler_cancel_after_claim_preserves_task_failure(monkeypatch):
    task_row = SimpleNamespace(
        status=TaskStatus.FAILED,
        verdict_status=VerdictStatus.FAILED,
        verdict_error="Cancelled by user",
    )
    monkeypatch.setattr(
        handlers_module,
        "get_session",
        _fake_get_session_factory(
            task_row,
            worker_status=WorkerJobStatus.CANCELLED,
        ),
    )

    async def fail_run(*_args, **_kwargs):
        pytest.fail("a cancelled QA worker must not execute")

    monkeypatch.setattr(handlers_module, "run_task_qa_job", fail_run)

    outcome = await QaJobHandler().run(_verdict_claim())

    assert outcome.success is not None
    assert task_row.status == TaskStatus.FAILED
    assert task_row.verdict_status == VerdictStatus.FAILED
    assert task_row.verdict_error == "Cancelled by user"


@pytest.mark.asyncio
async def test_qa_handler_duplicate_live_claim_retires_non_owner(monkeypatch):
    task_row = SimpleNamespace(
        status=TaskStatus.VERDICT_PENDING,
        current_version_id="task-v1",
        verdict_status=VerdictStatus.RUNNING,
        verdict_error=None,
        verdict_started_at="owner-start",
    )
    monkeypatch.setattr(
        handlers_module,
        "get_session",
        _fake_get_session_factory(task_row, qa_owner_id="wj-owner"),
    )

    async def fail_run(*_args, **_kwargs):
        pytest.fail("a duplicate non-owner must not run or steal the claim")

    monkeypatch.setattr(handlers_module, "run_task_qa_job", fail_run)

    outcome = await QaJobHandler().run(
        _verdict_claim(
            id="wj-duplicate",
            payload={
                "task_id": "task-xyz",
                "task_version_id": "task-v1",
                "task_version_content_hash": "hash-v1",
            },
        )
    )

    assert outcome.success is not None
    assert task_row.verdict_status == VerdictStatus.RUNNING
    assert task_row.verdict_started_at == "owner-start"


@pytest.mark.asyncio
async def test_qa_handler_retries_failed_current_pinned_verdict(monkeypatch):
    task_row = SimpleNamespace(
        status=TaskStatus.COMPLETED,
        current_version_id="task-v2",
        current_version_content_hash="hash-v2",
        verdict_status=VerdictStatus.FAILED,
        verdict_error="transient synth failure",
        verdict=None,
        verdict_started_at=None,
        verdict_finished_at="finished",
        finished_at="finished",
    )
    monkeypatch.setattr(
        handlers_module, "get_session", _fake_get_session_factory(task_row)
    )
    state_at_run = []

    async def rerun(*_args, **kwargs):
        state_at_run.append(task_row.verdict_status)
        assert kwargs["task_version_id"] == "task-v2"
        assert kwargs["task_version_content_hash"] == "hash-v2"
        task_row.verdict_status = VerdictStatus.SUCCESS
        task_row.verdict_error = None
        return False

    monkeypatch.setattr(handlers_module, "run_task_qa_job", rerun)

    outcome = await QaJobHandler().run(
        _verdict_claim(
            payload={
                "task_id": "task-xyz",
                "task_version_id": "task-v2",
                "task_version_content_hash": "hash-v2",
            }
        )
    )

    assert state_at_run == [VerdictStatus.QUEUED]
    assert outcome.success is not None


@pytest.mark.asyncio
async def test_qa_handler_retries_failed_explicitly_pinned_legacy_snapshot(
    monkeypatch,
):
    task_row = SimpleNamespace(
        status=TaskStatus.COMPLETED,
        current_version_id=None,
        verdict_status=VerdictStatus.FAILED,
        verdict_error="transient synth failure",
        verdict=None,
        verdict_started_at=None,
        verdict_finished_at="finished",
        finished_at="finished",
    )
    monkeypatch.setattr(
        handlers_module, "get_session", _fake_get_session_factory(task_row)
    )
    ran = []

    async def rerun(*_args, **kwargs):
        ran.append(kwargs["enforce_task_version_id"])
        task_row.verdict_status = VerdictStatus.SUCCESS
        return False

    monkeypatch.setattr(handlers_module, "run_task_qa_job", rerun)
    outcome = await QaJobHandler().run(
        _verdict_claim(
            payload={
                "task_id": "task-xyz",
                "task_version_id": None,
                "task_version_content_hash": None,
            }
        )
    )

    assert ran == [True]
    assert outcome.success is not None


@pytest.mark.asyncio
async def test_qa_handler_unpinned_failed_retry_reenters_current_admission(
    monkeypatch,
):
    task_row = SimpleNamespace(
        status=TaskStatus.VERDICT_PENDING,
        current_version_id="task-v1",
        current_version_content_hash="hash-v1",
        verdict_status=VerdictStatus.QUEUED,
        verdict_error=None,
        verdict=None,
        verdict_started_at=None,
        verdict_finished_at=None,
        finished_at=None,
    )
    monkeypatch.setattr(
        handlers_module, "get_session", _fake_get_session_factory(task_row)
    )
    run_count = 0
    admission_count = 0

    async def run_attempt(*_args, **kwargs):
        nonlocal run_count
        run_count += 1
        if run_count == 1:
            assert kwargs["enforce_task_version_id"] is True
            task_row.status = TaskStatus.COMPLETED
            task_row.verdict_status = VerdictStatus.FAILED
            task_row.verdict_error = "transient synth failure"
        else:
            assert kwargs["task_version_id"] == "task-v2"
            assert kwargs["task_version_content_hash"] == "hash-v2"
            task_row.verdict_status = VerdictStatus.SUCCESS
            task_row.verdict_error = None
        return False

    async def admit(_session, task_id, *, reuse_worker):
        nonlocal admission_count
        admission_count += 1
        assert task_id == "task-xyz"
        assert reuse_worker is True
        assert task_row.status == TaskStatus.RUNNING
        task_row.status = TaskStatus.VERDICT_PENDING
        task_row.verdict_status = VerdictStatus.QUEUED
        return TaskQAStageAdmission(
            advanced=True,
            reuse_worker=True,
            task_version_id="task-v1" if admission_count == 1 else "task-v2",
            task_version_content_hash=(
                "hash-v1" if admission_count == 1 else "hash-v2"
            ),
        )

    monkeypatch.setattr(handlers_module, "run_task_qa_job", run_attempt)
    monkeypatch.setattr("oddish.queue.maybe_start_task_qa_stage", admit)
    claim = _verdict_claim()

    first = await QaJobHandler().run(claim)
    assert first.failure is not None
    assert first.failure.retryable is True
    assert task_row.status == TaskStatus.COMPLETED

    second = await QaJobHandler().run(claim)

    assert second.success is not None
    assert admission_count == 2
    assert run_count == 2


@pytest.mark.asyncio
async def test_qa_handler_retires_superseded_version_without_retry(monkeypatch):
    task_row = SimpleNamespace(
        verdict_status=None,
        verdict_error=None,
    )
    monkeypatch.setattr(
        handlers_module, "get_session", _fake_get_session_factory(task_row)
    )
    pins = []

    async def _stub_run(*args, **kwargs):
        pins.append(kwargs["task_version_id"])
        assert kwargs["enforce_task_version_id"] is True
        return True

    async def _active_version_blocks(_session, task_id, *, reuse_worker):
        assert task_id == "task-xyz"
        assert reuse_worker is True
        return TaskQAStageAdmission()

    monkeypatch.setattr(handlers_module, "run_task_qa_job", _stub_run)
    monkeypatch.setattr(
        "oddish.queue.maybe_start_task_qa_stage", _active_version_blocks
    )

    outcome = await QaJobHandler().run(
        _verdict_claim(payload={"task_id": "task-xyz", "task_version_id": "task-v1"})
    )

    assert pins == ["task-v1"]
    assert outcome.success is not None
    assert outcome.success.result_summary is None


@pytest.mark.asyncio
async def test_qa_handler_reuses_worker_after_current_version_admission(monkeypatch):
    task_row = SimpleNamespace(
        verdict_status=None,
        verdict_error=None,
    )
    monkeypatch.setattr(
        handlers_module, "get_session", _fake_get_session_factory(task_row)
    )
    pins = []

    async def _stub_run(*args, **kwargs):
        pins.append(kwargs["task_version_id"])
        assert kwargs["enforce_task_version_id"] is True
        if len(pins) == 1:
            return True
        task_row.verdict_status = VerdictStatus.SUCCESS
        return False

    async def _reuse(_session, task_id, *, reuse_worker):
        return TaskQAStageAdmission(
            advanced=True,
            reuse_worker=True,
            task_version_id="task-v2",
            task_version_content_hash="hash-v2",
        )

    monkeypatch.setattr(handlers_module, "run_task_qa_job", _stub_run)
    monkeypatch.setattr("oddish.queue.maybe_start_task_qa_stage", _reuse)

    outcome = await QaJobHandler().run(
        _verdict_claim(payload={"task_id": "task-xyz", "task_version_id": "task-v1"})
    )

    assert pins == ["task-v1", "task-v2"]
    assert outcome.success is not None


@pytest.mark.asyncio
async def test_qa_handler_legacy_unpinned_retry_is_admitted_before_run(monkeypatch):
    task_row = SimpleNamespace(
        status=TaskStatus.VERDICT_PENDING,
        finished_at=None,
        verdict=None,
        verdict_status=VerdictStatus.QUEUED,
        verdict_error=None,
        verdict_started_at=None,
        verdict_finished_at=None,
    )
    monkeypatch.setattr(
        handlers_module, "get_session", _fake_get_session_factory(task_row)
    )
    calls = []

    async def _admit(_session, task_id, *, reuse_worker):
        assert task_row.status == TaskStatus.RUNNING
        return TaskQAStageAdmission(
            advanced=True,
            reuse_worker=True,
            task_version_id="task-current-v2",
            task_version_content_hash="hash-current-v2",
        )

    async def _stub_run(*_args, **kwargs):
        calls.append(
            (
                kwargs["task_version_id"],
                kwargs["enforce_task_version_id"],
                kwargs["task_version_content_hash"],
                kwargs["enforce_task_version_content_hash"],
            )
        )
        task_row.verdict_status = VerdictStatus.SUCCESS
        return False

    monkeypatch.setattr("oddish.queue.maybe_start_task_qa_stage", _admit)
    monkeypatch.setattr(handlers_module, "run_task_qa_job", _stub_run)

    outcome = await QaJobHandler().run(_verdict_claim())

    assert calls == [("task-current-v2", True, "hash-current-v2", True)]
    assert outcome.success is not None


@pytest.mark.asyncio
async def test_qa_handler_rechecks_pin_across_second_version_switch(monkeypatch):
    task_row = SimpleNamespace(
        verdict_status=None,
        verdict_error=None,
    )
    monkeypatch.setattr(
        handlers_module, "get_session", _fake_get_session_factory(task_row)
    )
    pins = []
    admitted_versions = iter(("task-v2", "task-v3"))
    admitted_hashes = iter(("hash-v2", "hash-v3"))

    async def _stub_run(*args, **kwargs):
        pins.append(kwargs["task_version_id"])
        assert kwargs["enforce_task_version_id"] is True
        if len(pins) < 3:
            return True
        task_row.verdict_status = VerdictStatus.SUCCESS
        return False

    async def _reuse(_session, task_id, *, reuse_worker):
        return TaskQAStageAdmission(
            advanced=True,
            reuse_worker=True,
            task_version_id=next(admitted_versions),
            task_version_content_hash=next(admitted_hashes),
        )

    monkeypatch.setattr(handlers_module, "run_task_qa_job", _stub_run)
    monkeypatch.setattr("oddish.queue.maybe_start_task_qa_stage", _reuse)

    outcome = await QaJobHandler().run(
        _verdict_claim(payload={"task_id": "task-xyz", "task_version_id": "task-v1"})
    )

    assert pins == ["task-v1", "task-v2", "task-v3"]
    assert outcome.success is not None


@pytest.mark.asyncio
async def test_qa_handler_admission_failure_escapes_for_worker_retry(monkeypatch):
    task_row = SimpleNamespace(verdict_status=None, verdict_error=None)
    monkeypatch.setattr(
        handlers_module, "get_session", _fake_get_session_factory(task_row)
    )

    async def _superseded(*_args, **_kwargs):
        return True

    async def _admission_failed(*_args, **_kwargs):
        raise RuntimeError("transient admission database failure")

    monkeypatch.setattr(handlers_module, "run_task_qa_job", _superseded)
    monkeypatch.setattr("oddish.queue.maybe_start_task_qa_stage", _admission_failed)

    with pytest.raises(RuntimeError, match="transient admission database failure"):
        await QaJobHandler().run(
            _verdict_claim(
                payload={"task_id": "task-xyz", "task_version_id": "task-v1"}
            )
        )


def _qa_failure_outcome(monkeypatch, verdict_error: str):
    task_row = SimpleNamespace(
        verdict_status=VerdictStatus.RUNNING,
        verdict_error=None,
    )
    monkeypatch.setattr(
        handlers_module, "get_session", _fake_get_session_factory(task_row)
    )

    async def _stub_run(*args, **kwargs):
        task_row.verdict_status = VerdictStatus.FAILED
        task_row.verdict_error = verdict_error

    monkeypatch.setattr(handlers_module, "run_task_qa_job", _stub_run)
    return QaJobHandler().run(_verdict_claim())


@pytest.mark.asyncio
async def test_qa_handler_returns_retryable_fail_on_ordinary_error(monkeypatch):
    outcome = await _qa_failure_outcome(monkeypatch, "ValueError: no classifications")

    assert outcome.failure.retryable is True


@pytest.mark.asyncio
async def test_qa_handler_returns_permanent_fail_on_provider_403(monkeypatch):
    """Retrying a provider permission block never clears it -- prod burned
    6/6 attempts on every QA job for 58 hours and re-hit the blocked Azure
    resource 26x per task."""
    outcome = await _qa_failure_outcome(
        monkeypatch,
        "PermissionDeniedError: Error code: 403 - {'error': {'message': 'Your "
        "resource has been temporarily blocked because we detected behavior "
        "that may violate our content policy.'}}",
    )

    assert outcome.failure.retryable is False


@pytest.mark.asyncio
async def test_qa_handler_permanent_when_fallback_fails_after_403(monkeypatch):
    """Both verdict models failing is still permanent: a retry would run the
    blocked primary again before reaching the fallback."""
    outcome = await _qa_failure_outcome(
        monkeypatch,
        "TimeoutError: claude timed out [verdict fallback "
        "anthropic/claude-sonnet-5 failed after permanent primary failure -- "
        "PermissionDeniedError: Error code: 403 - resource blocked]",
    )

    assert outcome.failure.retryable is False


# ---------------------------------------------------------------------------
# Handler registry side effects
# ---------------------------------------------------------------------------


def test_all_three_handlers_register_against_builtin_registry():
    from oddish.workers.jobs import (
        HANDLERS,
        ensure_builtin_handlers_registered,
    )

    ensure_builtin_handlers_registered()
    assert WorkerJobKind.TRIAL in HANDLERS
    assert WorkerJobKind.QA in HANDLERS
    # ANALYSIS handler is kept transitionally to drain legacy rows.
    assert WorkerJobKind.ANALYSIS in HANDLERS


def test_tag_project_handler_is_registered(monkeypatch):
    from oddish.db import WorkerJobKind
    from oddish.workers.jobs import (
        ensure_builtin_handlers_registered,
        get_handler,
    )

    ensure_builtin_handlers_registered()
    handler = get_handler(WorkerJobKind.TAG_PROJECT)
    assert handler.kind == WorkerJobKind.TAG_PROJECT


def test_tag_project_handler_validate_payload_requires_scope_and_target():
    from oddish.workers.jobs.handlers import TagProjectJobHandler

    h = TagProjectJobHandler()
    h.validate_payload({"scope": "TASK", "target_id": "t-1", "mode": "direct"})
    import pytest

    with pytest.raises(ValueError):
        h.validate_payload({"mode": "direct"})
    with pytest.raises(ValueError):
        h.validate_payload({"scope": "TASK", "target_id": ""})

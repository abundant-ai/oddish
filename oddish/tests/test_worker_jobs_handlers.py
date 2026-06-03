"""Tests for the per-kind JobHandler wrappers in ``oddish.workers.jobs.handlers``.

The handlers are thin adapters: they delegate to the existing
``run_trial_job`` / ``run_analysis_job`` / ``run_verdict_job``
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
    TrialStatus,
    VerdictStatus,
    WorkerJobKind,
)
from oddish.workers.jobs import handlers as handlers_module  # noqa: E402
from oddish.workers.jobs.handlers import (  # noqa: E402
    AnalysisJobHandler,
    TrialJobHandler,
    VerdictJobHandler,
)
from oddish.workers.queue.worker_job_single_job import ClaimedWorkerJob  # noqa: E402


def _fake_get_session_factory(domain_row):
    """Build a ``get_session``-compatible context manager for tests."""

    class _Session:
        async def get(self, model, obj_id):
            if domain_row is None:
                return None
            return domain_row

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
        kind=WorkerJobKind.VERDICT,
        queue_key="verdict",
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
# VerdictJobHandler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verdict_handler_returns_ok_on_success(monkeypatch):
    task_row = SimpleNamespace(
        verdict_status=VerdictStatus.RUNNING,
        verdict_error=None,
    )
    monkeypatch.setattr(
        handlers_module, "get_session", _fake_get_session_factory(task_row)
    )

    async def _stub_run(*args, **kwargs):
        task_row.verdict_status = VerdictStatus.SUCCESS

    monkeypatch.setattr(handlers_module, "run_verdict_job", _stub_run)

    outcome = await VerdictJobHandler().run(_verdict_claim())

    assert outcome.success is not None


@pytest.mark.asyncio
async def test_verdict_handler_resets_terminal_state_on_retry(monkeypatch):
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

    monkeypatch.setattr(handlers_module, "run_verdict_job", _stub_run)

    outcome = await VerdictJobHandler().run(_verdict_claim())

    assert state_at_run["status"] == VerdictStatus.QUEUED
    assert outcome.success is not None


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
    assert WorkerJobKind.ANALYSIS in HANDLERS
    assert WorkerJobKind.VERDICT in HANDLERS

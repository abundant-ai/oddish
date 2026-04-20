"""Tests for the Phase A `worker_jobs` handler registry.

The registry is the seam between the unified dispatcher and the per-kind
execution code. These tests pin the Protocol-based contract (register /
lookup / double-registration / clear) and the ``JobOutcome`` validation
that keeps later phases from accidentally returning "neither success nor
failure".
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.db import WorkerJobKind  # noqa: E402
from oddish.workers.jobs import (  # noqa: E402
    HANDLERS,
    HandlerAlreadyRegisteredError,
    JobFailure,
    JobHandler,
    JobOutcome,
    JobSuccess,
    NoHandlerRegisteredError,
    clear_handlers,
    get_handler,
    register,
)


class _FakeHandler:
    def __init__(self, kind: WorkerJobKind, *, queue_key: str = "default") -> None:
        self.kind = kind
        self._queue_key = queue_key

    def default_queue_key(self, job):  # type: ignore[override]
        return self._queue_key

    def validate_payload(self, payload):  # type: ignore[override]
        return payload

    async def run(self, job):  # type: ignore[override]
        return JobOutcome.ok()


@pytest.fixture(autouse=True)
def _reset_registry():
    clear_handlers()
    yield
    clear_handlers()


# ---------------------------------------------------------------------------
# JobOutcome
# ---------------------------------------------------------------------------


def test_job_outcome_ok_sets_success_only():
    outcome = JobOutcome.ok({"answer": 42})
    assert isinstance(outcome.success, JobSuccess)
    assert outcome.success.result_summary == {"answer": 42}
    assert outcome.failure is None


def test_job_outcome_fail_sets_failure_only():
    outcome = JobOutcome.fail("boom", retryable=False)
    assert outcome.success is None
    assert isinstance(outcome.failure, JobFailure)
    assert outcome.failure.error_message == "boom"
    assert outcome.failure.retryable is False


def test_job_outcome_rejects_both_unset():
    with pytest.raises(ValueError, match="exactly one"):
        JobOutcome()


def test_job_outcome_rejects_both_set():
    with pytest.raises(ValueError, match="exactly one"):
        JobOutcome(success=JobSuccess(), failure=JobFailure("no"))


def test_job_failure_default_is_retryable():
    outcome = JobOutcome.fail("transient")
    assert outcome.failure is not None
    assert outcome.failure.retryable is True


# ---------------------------------------------------------------------------
# Registry: register / get / clear
# ---------------------------------------------------------------------------


def test_register_adds_handler_to_global_dict():
    handler = _FakeHandler(WorkerJobKind.TRIAL)
    register(handler)
    assert HANDLERS[WorkerJobKind.TRIAL] is handler


def test_get_handler_returns_registered_instance():
    handler = _FakeHandler(WorkerJobKind.ANALYSIS)
    register(handler)
    assert get_handler(WorkerJobKind.ANALYSIS) is handler


def test_get_handler_raises_when_unregistered():
    with pytest.raises(NoHandlerRegisteredError):
        get_handler(WorkerJobKind.VERDICT)


def test_register_is_idempotent_for_same_object():
    handler = _FakeHandler(WorkerJobKind.TRIAL)
    register(handler)
    # Same object -> no-op, no exception.
    register(handler)
    assert HANDLERS[WorkerJobKind.TRIAL] is handler


def test_register_rejects_different_handler_for_same_kind():
    first = _FakeHandler(WorkerJobKind.TRIAL)
    second = _FakeHandler(WorkerJobKind.TRIAL)
    register(first)
    with pytest.raises(HandlerAlreadyRegisteredError):
        register(second)
    assert HANDLERS[WorkerJobKind.TRIAL] is first


def test_clear_handlers_empties_registry():
    register(_FakeHandler(WorkerJobKind.TRIAL))
    register(_FakeHandler(WorkerJobKind.ANALYSIS))
    clear_handlers()
    assert HANDLERS == {}


def test_register_returns_handler_for_decorator_use():
    handler = _FakeHandler(WorkerJobKind.QA_REVIEW)
    assert register(handler) is handler


def test_fake_handler_satisfies_protocol():
    # Structural protocol check -- if this breaks, the Protocol surface
    # changed and handlers elsewhere need the same update.
    handler = _FakeHandler(WorkerJobKind.TRIAL)
    assert isinstance(handler, JobHandler)


# ---------------------------------------------------------------------------
# Handler `run` return type is awaitable + yields a JobOutcome
# ---------------------------------------------------------------------------


def test_handler_run_returns_job_outcome():
    handler = _FakeHandler(WorkerJobKind.TRIAL)
    outcome = asyncio.run(handler.run(object()))  # job stub is opaque in Phase A
    assert isinstance(outcome, JobOutcome)
    assert outcome.success is not None

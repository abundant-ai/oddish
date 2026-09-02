"""Provider account caps must settle trial and worker-job state consistently."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from oddish.db import TrialStatus, WorkerJobKind
from oddish.workers.harbor.runner import HarborOutcome
from oddish.workers.jobs import handlers
from oddish.workers.jobs.handlers import TrialJobHandler
from oddish.workers.queue import trial_handler
from oddish.workers.queue.provider_failures import classify_provider_failure
from oddish.workers.queue.worker_job_single_job import ClaimedWorkerJob


USAGE_CAP = (
    "API Error: 400 You have reached your specified API usage limits. "
    "You will regain access on 2099-09-01 at 00:00 UTC.\n"
)


def _outcome(error: str, exception_type: str | None = None) -> HarborOutcome:
    return HarborOutcome(
        reward=None,
        error=error,
        exit_code=1,
        duration_sec=1.0,
        job_result_path=None,
        job_dir=None,
        exception_type=exception_type,
    )


@pytest.mark.parametrize(
    ("error", "exception_type", "terminal"),
    [
        (USAGE_CAP, None, True),
        ("PermissionDeniedError: Error code: 403 - forbidden", None, True),
        ("RateLimitError: Error code: 429 - slow down", None, False),
        ("TimeoutError: provider timed out", None, False),
        (
            "BadRequestError: Error code: 400 - Your credit balance is too low",
            None,
            False,
        ),
    ],
)
def test_provider_failure_retry_policy(error, exception_type, terminal):
    assert (
        trial_handler._is_non_retryable_outcome(
            SimpleNamespace(harbor_config={}),
            _outcome(error, exception_type),
        )
        is terminal
    )


@pytest.mark.asyncio
async def test_future_usage_cap_fails_trial_on_first_attempt_and_worker_agrees(
    monkeypatch,
):
    original_error = USAGE_CAP + ("provider diagnostic detail " * 40)
    trial = SimpleNamespace(
        id="trial-usage-cap",
        task_id="task-usage-cap",
        kind="audit",
        agent="claude-code",
        model="claude-sonnet-4-6",
        harbor_config={},
        status=TrialStatus.RUNNING,
        attempts=1,
        max_attempts=3,
        error_message=None,
        harbor_stage="agent",
        reward=None,
        harbor_result_path=None,
        trial_s3_key=None,
        input_tokens=None,
        cache_tokens=None,
        cache_write_tokens=None,
        output_tokens=None,
        cost_usd=None,
        phase_timing=None,
        result=None,
        has_trajectory=False,
        current_worker_id="worker-1",
        current_queue_slot=0,
        heartbeat_at=None,
        finished_at=None,
        superseded_by_trial_id=None,
        deleted_at=None,
    )

    @asynccontextmanager
    async def fake_trial_session(
        _trial_id: str, *, allow_missing: bool = False, with_for_update: bool = False
    ):
        yield object(), trial

    monkeypatch.setattr(trial_handler, "_trial_session", fake_trial_session)
    monkeypatch.setattr(
        trial_handler, "refresh_task_browse_summaries", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        trial_handler, "_log_trial_metering_integrity", lambda *_a, **_k: None
    )

    terminal, completed = await trial_handler._store_trial_results(
        trial_id=trial.id,
        outcome=_outcome(original_error),
        trial_s3_key=None,
        execution_error=None,
        trial_attempt=1,
    )

    assert (terminal, completed) == (True, True)
    assert trial.status == TrialStatus.FAILED
    assert trial.finished_at is not None
    assert trial.attempts == 1
    assert trial.error_message == classify_provider_failure(original_error).error_summary

    @asynccontextmanager
    async def fake_handler_session():
        yield SimpleNamespace(get=AsyncMock(return_value=trial))

    monkeypatch.setattr(handlers, "get_session", fake_handler_session)
    monkeypatch.setattr(handlers, "run_trial_job", AsyncMock(return_value=None))
    monkeypatch.setattr(handlers, "decrypt_credentials", lambda _value: None)
    job = ClaimedWorkerJob(
        id="worker-job-usage-cap",
        kind=WorkerJobKind.TRIAL,
        queue_key="anthropic/claude-sonnet-4-6",
        subject_table="trials",
        subject_id=trial.id,
        payload={},
        attempts=1,
        max_attempts=3,
        org_id=None,
        parent_job_id=None,
        worker_id="worker-1",
        queue_slot=0,
    )

    worker_outcome = await TrialJobHandler().run(job)

    assert worker_outcome.failure is not None
    assert worker_outcome.failure.retryable is False

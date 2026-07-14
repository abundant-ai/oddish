"""Cancellation semantics for the analyzer generation job.

The trial-analyses upload is a best-effort side product that runs *after* the
primary eval has already produced ``output``. A cancel landing there must not
discard that completed analysis, while a cancel landing *before* the eval
finishes is still a real failure.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from oddish.db.models import JobStatus
from oddish.workers.queue import analyzer_handler


class _FakeAnalyzer:
    def __init__(self) -> None:
        self.status = JobStatus.QUEUED
        self.started_at = None
        self.finished_at = None
        self.org_id = "org-1"
        self.save_trial_analyses = True
        self.error = None
        self.bad_failure_content = None
        self.good_failure_content = None
        self.universal_capabilities_content = None
        self.headroom_analysis = None
        self.num_trials = None
        self.num_bad_failures = None
        self.num_good_failures = None
        self.breakdown = None


class _FakeOutput:
    findings: list = []
    counts = {"trials": 3, "bad": 1, "good": 2}
    breakdown = {"by_task": {}}
    sections = {
        "bad": "bad-section",
        "good": "good-section",
        "capabilities": "capabilities-section",
        "headroom": "headroom-section",
    }


class _FakeInputs:
    subanalyses: list = []


@pytest.fixture
def analyzer(monkeypatch):
    """Wire the handler up with in-memory doubles: no DB, no S3, no model calls."""
    row = _FakeAnalyzer()

    @asynccontextmanager
    async def _session():
        class _S:
            async def get(self, *_args, **_kwargs):
                return row

            async def scalar(self, *_args, **_kwargs):
                return None

            async def commit(self):
                return None

        yield _S()

    async def _gather(*_args, **_kwargs):
        return []

    async def _inputs(*_args, **_kwargs):
        return _FakeInputs()

    monkeypatch.setattr(analyzer_handler, "get_session", lambda: _session())
    monkeypatch.setattr(analyzer_handler, "_gather_trial_rows", _gather)
    monkeypatch.setattr(analyzer_handler, "build_analyzer_inputs", _inputs)
    monkeypatch.setattr(
        analyzer_handler, "_build_analyzer_eval_config", lambda: object()
    )
    monkeypatch.setattr(
        analyzer_handler, "build_trial_analyses_payload", lambda **_k: {"trials": [{}]}
    )
    return row


@pytest.mark.asyncio
async def test_cancel_during_trial_upload_keeps_successful_eval(analyzer, monkeypatch):
    """A cancel in the best-effort S3 upload must not lose the finished analysis.

    CancelledError is a BaseException, so it escapes the ``except Exception`` in
    _maybe_save_trial_analyses and reaches the job's cancel handler -- which must
    keep the already-computed output rather than marking the job FAILED.
    """

    async def _eval(*_args, **_kwargs):
        return _FakeOutput()

    class _CancellingStorage:
        async def upload_bytes(self, *_args, **_kwargs):
            raise asyncio.CancelledError()

    monkeypatch.setattr(analyzer_handler, "run_analyzer_eval", _eval)
    monkeypatch.setattr(analyzer_handler, "get_storage_client", _CancellingStorage)

    await analyzer_handler.run_analyzer_generation_job("an-1", worker_job_id=None)

    assert analyzer.status == JobStatus.SUCCESS
    assert analyzer.error is None
    assert analyzer.bad_failure_content == "bad-section"
    assert analyzer.num_trials == 3


@pytest.mark.asyncio
async def test_cancel_during_eval_still_fails_the_job(analyzer, monkeypatch):
    """A cancel *before* the eval produces output is a genuine failure."""

    async def _eval(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(analyzer_handler, "run_analyzer_eval", _eval)

    await analyzer_handler.run_analyzer_generation_job("an-1", worker_job_id=None)

    assert analyzer.status == JobStatus.FAILED
    assert "cancelled" in (analyzer.error or "")

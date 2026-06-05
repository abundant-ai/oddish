"""Regression tests for the analysis-enqueue guard.

A trial with no stored result (neither ``trial_s3_key`` nor
``harbor_result_path``) can never be analyzed -- the handler raises
"No trial location available" and burns every retry. ``enqueue_analysis_worker_job``
must skip such trials (marking analysis FAILED) instead of enqueuing a doomed
job onto the shared analysis queue.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import oddish.queue as queue_module  # noqa: E402
from oddish.db import AnalysisStatus, WorkerJobKind  # noqa: E402


class _FakeSession:
    def __init__(self, trial: SimpleNamespace) -> None:
        self._trial = trial

    async def get(self, model, object_id):  # noqa: ANN001
        return self._trial


@pytest.mark.asyncio
async def test_enqueue_analysis_skips_trial_without_result(monkeypatch):
    trial = SimpleNamespace(
        id="trial-1",
        trial_s3_key=None,
        harbor_result_path=None,
        analysis_status=AnalysisStatus.QUEUED,
        analysis_error=None,
        analysis_finished_at=None,
    )
    session = _FakeSession(trial)

    async def _fail_enqueue(*_args, **_kwargs):
        raise AssertionError("must not enqueue analysis for a trial with no result")

    monkeypatch.setattr(queue_module, "enqueue_worker_job", _fail_enqueue)

    job = await queue_module.enqueue_analysis_worker_job(
        session, trial_id="trial-1", org_id="org-1"
    )

    assert job is None
    assert trial.analysis_status == AnalysisStatus.FAILED
    assert trial.analysis_error == queue_module.ANALYSIS_NO_RESULT_MESSAGE
    assert trial.analysis_finished_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trial_s3_key, harbor_result_path",
    [
        ("trials/trial-2/result.tar.gz", None),
        (None, "/tmp/harbor-jobs/trial-2/result"),
    ],
)
async def test_enqueue_analysis_runs_when_result_present(
    monkeypatch, trial_s3_key, harbor_result_path
):
    trial = SimpleNamespace(
        id="trial-2",
        trial_s3_key=trial_s3_key,
        harbor_result_path=harbor_result_path,
        analysis_status=AnalysisStatus.QUEUED,
    )
    session = _FakeSession(trial)

    captured: dict[str, object] = {}
    sentinel = object()

    async def _ok_enqueue(_session, request):  # noqa: ANN001
        captured["request"] = request
        return sentinel

    monkeypatch.setattr(queue_module, "enqueue_worker_job", _ok_enqueue)

    job = await queue_module.enqueue_analysis_worker_job(
        session, trial_id="trial-2", org_id="org-1"
    )

    assert job is sentinel
    request = captured["request"]
    assert request.kind == WorkerJobKind.ANALYSIS
    assert request.subject_id == "trial-2"
    # Status untouched -- the caller's QUEUED stands when we actually enqueue.
    assert trial.analysis_status == AnalysisStatus.QUEUED

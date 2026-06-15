"""Tests for the unified task-level QA pipeline.

Trajectory analysis is now a single task-scoped job: when every trial in a
task finishes, one ``VERDICT`` worker job classifies all live trials and then
synthesizes the task verdict. These tests cover:

* the candidate-selection + verdict-reconstruction helpers,
* the stage transition that enqueues exactly one QA job (and no per-trial
  ANALYSIS jobs), and
* ``run_verdict_job`` orchestrating classification + verdict in one job.
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import oddish.queue as queue_mod  # noqa: E402
from oddish.db import (  # noqa: E402
    AnalysisStatus,
    TaskStatus,
    VerdictStatus,
    WorkerJobKind,
)
from oddish.workers.queue import verdict_handler  # noqa: E402
from oddish.workers.queue.verdict_handler import (  # noqa: E402
    _classifications_from_trials,
    _trial_needs_classification,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "analysis_status, expected",
    [
        (None, True),
        (AnalysisStatus.QUEUED, True),
        (AnalysisStatus.PENDING, True),
        (AnalysisStatus.RUNNING, True),
        # Already terminal -> reused, not re-classified.
        (AnalysisStatus.SUCCESS, False),
        (AnalysisStatus.FAILED, False),
    ],
)
def test_trial_needs_classification(analysis_status, expected):
    assert _trial_needs_classification(analysis_status) is expected


def test_classifications_from_trials_filters_probe_and_unfinished():
    trials = [
        SimpleNamespace(
            id="t-0",
            analysis_status=AnalysisStatus.SUCCESS,
            analysis={
                "classification": "GOOD_SUCCESS",
                "subtype": "Clean",
                "evidence": "ev",
                "root_cause": "rc",
                "recommendation": "rec",
                "reward": 1.0,
            },
        ),
        # FAILED analysis -> excluded.
        SimpleNamespace(
            id="t-1",
            analysis_status=AnalysisStatus.FAILED,
            analysis=None,
        ),
        # Probe summary stored as SUCCESS but no "classification" key -> skipped.
        SimpleNamespace(
            id="t-2",
            analysis_status=AnalysisStatus.SUCCESS,
            analysis={"headline": "probe", "summary": "..."},
        ),
        SimpleNamespace(
            id="t-3",
            analysis_status=AnalysisStatus.SUCCESS,
            analysis={"classification": "BAD_FAILURE", "subtype": "Flaky"},
        ),
    ]

    classifications = _classifications_from_trials(trials)

    names = [(c.trial_name, c.classification.value) for c in classifications]
    assert names == [("t-0", "GOOD_SUCCESS"), ("t-3", "BAD_FAILURE")]


# ---------------------------------------------------------------------------
# Stage transition: enqueue exactly one task-level QA job
# ---------------------------------------------------------------------------


class _ForUpdateResult:
    def __init__(self, task):
        self._task = task

    def scalar_one_or_none(self):
        return self._task


class _StageSession:
    """Minimal session for ``maybe_start_analysis_stage``."""

    def __init__(self, *, trial, task, pending_count):
        self._trial = trial
        self._task = task
        self._pending_count = pending_count
        self.flushed = 0

    async def get(self, _model, _key):
        return self._trial

    async def execute(self, _statement):
        return _ForUpdateResult(self._task)

    async def scalar(self, _statement):
        return self._pending_count

    async def flush(self):
        self.flushed += 1


@pytest.mark.asyncio
async def test_stage_enqueues_single_qa_job_when_trials_done(monkeypatch):
    trial = SimpleNamespace(task_id="task-1")
    task = SimpleNamespace(
        id="task-1",
        org_id="org-1",
        status=TaskStatus.RUNNING,
        run_analysis=True,
        verdict_status=None,
        finished_at=None,
    )
    session = _StageSession(trial=trial, task=task, pending_count=0)

    verdict_calls: list[str] = []

    async def fake_verdict_enqueue(_session, *, task_id, org_id):
        verdict_calls.append(task_id)

    async def fail_analysis_enqueue(*_args, **_kwargs):
        raise AssertionError("the unified pipeline must not enqueue per-trial analysis")

    monkeypatch.setattr(queue_mod, "enqueue_verdict_worker_job", fake_verdict_enqueue)
    monkeypatch.setattr(
        queue_mod, "enqueue_analysis_worker_job", fail_analysis_enqueue
    )

    started = await queue_mod.maybe_start_analysis_stage(session, "task-1-0")

    assert started is True
    assert verdict_calls == ["task-1"]
    assert task.status == TaskStatus.VERDICT_PENDING
    assert task.verdict_status == VerdictStatus.QUEUED


@pytest.mark.asyncio
async def test_stage_completes_when_analysis_disabled(monkeypatch):
    trial = SimpleNamespace(task_id="task-2")
    task = SimpleNamespace(
        id="task-2",
        org_id="org-1",
        status=TaskStatus.RUNNING,
        run_analysis=False,
        verdict_status=None,
        finished_at=None,
    )
    session = _StageSession(trial=trial, task=task, pending_count=0)

    async def fail_verdict_enqueue(*_args, **_kwargs):
        raise AssertionError("no QA job when run_analysis is disabled")

    monkeypatch.setattr(queue_mod, "enqueue_verdict_worker_job", fail_verdict_enqueue)

    started = await queue_mod.maybe_start_analysis_stage(session, "task-2-0")

    assert started is True
    assert task.status == TaskStatus.COMPLETED


# ---------------------------------------------------------------------------
# run_verdict_job: one job classifies all trials, then synthesizes the verdict
# ---------------------------------------------------------------------------


class _ScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _QASession:
    def __init__(self, *, task, trials):
        self._task = task
        self._trials = trials

    async def get(self, _model, _key):
        return self._task

    async def execute(self, _statement):
        return _ScalarsResult(self._trials)


@pytest.mark.asyncio
async def test_run_verdict_job_classifies_then_synthesizes(monkeypatch):
    task = SimpleNamespace(
        id="task-9",
        org_id="org-1",
        status=TaskStatus.VERDICT_PENDING,
        verdict_status=VerdictStatus.QUEUED,
        verdict=None,
        verdict_error=None,
        verdict_started_at=None,
        verdict_finished_at=None,
        finished_at=None,
    )
    # Two normal trials needing classification, one probe (skipped), one already
    # SUCCESS (reused, not re-classified).
    trials = {
        "task-9-0": SimpleNamespace(
            id="task-9-0", analysis_status=None, analysis=None
        ),
        "task-9-1": SimpleNamespace(
            id="task-9-1", analysis_status=None, analysis=None
        ),
        "task-9-2": SimpleNamespace(
            id="task-9-2",
            analysis_status=AnalysisStatus.SUCCESS,
            analysis={"headline": "probe"},
        ),
        "task-9-3": SimpleNamespace(
            id="task-9-3",
            analysis_status=AnalysisStatus.SUCCESS,
            analysis={"classification": "GOOD_SUCCESS", "subtype": "Clean"},
        ),
    }
    session = _QASession(task=task, trials=list(trials.values()))

    @asynccontextmanager
    async def fake_get_session():
        yield session

    async def fake_load_live(_task_id):
        return [
            ("task-9-0", None),
            ("task-9-1", None),
            ("task-9-2", AnalysisStatus.SUCCESS),  # probe, inline -> reused
            ("task-9-3", AnalysisStatus.SUCCESS),  # already done -> reused
        ]

    classified: list[str] = []

    async def fake_classify(trial_id):
        classified.append(trial_id)
        trials[trial_id].analysis_status = AnalysisStatus.SUCCESS
        trials[trial_id].analysis = {
            "classification": "BAD_FAILURE",
            "subtype": "Flaky",
        }

    captured = {"classifications": None}

    def fake_compute_verdict(*, classifications, **_kwargs):
        captured["classifications"] = classifications
        return SimpleNamespace(
            is_good=False,
            confidence="high",
            primary_issue="task issue",
            reasoning="because",
            recommendations=["fix it"],
            task_problem_count=1,
            agent_problem_count=0,
            success_count=1,
            harness_error_count=0,
        )

    monkeypatch.setattr(verdict_handler, "get_session", fake_get_session)
    monkeypatch.setattr(
        verdict_handler, "_load_live_trials_for_classification", fake_load_live
    )
    monkeypatch.setattr(verdict_handler, "classify_trial_and_store", fake_classify)
    monkeypatch.setattr("oddish.analyze.compute_task_verdict", fake_compute_verdict)

    await verdict_handler.run_verdict_job("task-9", queue_key="verdict")

    # Only the two unfinished, non-probe trials are classified.
    assert sorted(classified) == ["task-9-0", "task-9-1"]
    # Verdict synthesized from all SUCCESS, non-probe classifications (3 of them).
    assert captured["classifications"] is not None
    assert len(captured["classifications"]) == 3
    assert task.verdict_status == VerdictStatus.SUCCESS
    assert task.status == TaskStatus.COMPLETED
    assert task.verdict["is_good"] is False
    assert task.verdict["primary_issue"] == "task issue"


@pytest.mark.asyncio
async def test_run_verdict_job_skips_when_already_terminal(monkeypatch):
    task = SimpleNamespace(
        id="task-10",
        verdict_status=VerdictStatus.SUCCESS,
    )
    session = _QASession(task=task, trials=[])

    @asynccontextmanager
    async def fake_get_session():
        yield session

    async def fail_classify(_trial_id):
        raise AssertionError("must not classify when verdict already terminal")

    monkeypatch.setattr(verdict_handler, "get_session", fake_get_session)
    monkeypatch.setattr(verdict_handler, "classify_trial_and_store", fail_classify)

    # Should return early without touching classification.
    await verdict_handler.run_verdict_job("task-10", queue_key="verdict")
    assert task.verdict_status == VerdictStatus.SUCCESS


def test_verdict_job_uses_verdict_kind():
    assert WorkerJobKind.VERDICT.value == "VERDICT"

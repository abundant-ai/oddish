"""Tests for the unified task-level QA pipeline.

Trajectory analysis is now a single task-scoped job: when every trial in a
task finishes, one ``QA`` worker job classifies all live trials and then
synthesizes the task verdict. These tests cover:

* the candidate-selection + verdict-reconstruction helpers,
* the stage transition that enqueues exactly one QA job (and no per-trial
  ANALYSIS jobs), and
* ``run_task_qa_job`` orchestrating classification + verdict in one job.
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
    WorkerJobStatus,
)
from oddish.workers.queue import qa_handler  # noqa: E402
from oddish.workers.queue.qa_handler import (  # noqa: E402
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
    """Minimal session for ``maybe_start_qa_stage``.

    The function issues its counting queries in a fixed order:

    1. ``pending_count``  -- non-terminal trials; gates "are all trials done?"
    2. ``qa_eligible``    -- QA-eligible live trials (only when run_analysis is
       on); gates "is there anything to classify?" Zero means every live trial
       is excluded (bulk-migrated import / gate-skipped), so no QA job is
       enqueued and the task completes instead.

    ``scalar`` therefore answers positionally rather than returning one canned
    number for every query.
    """

    def __init__(self, *, trial, task, pending_count, qa_eligible=1):
        self._trial = trial
        self._task = task
        self._counts = [pending_count, qa_eligible]
        self._scalar_calls = 0
        self.flushed = 0

    async def get(self, _model, _key):
        return self._trial

    async def execute(self, _statement):
        return _ForUpdateResult(self._task)

    async def scalar(self, _statement):
        index = self._scalar_calls
        self._scalar_calls += 1
        return self._counts[index] if index < len(self._counts) else 0

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

    monkeypatch.setattr(queue_mod, "enqueue_qa_worker_job", fake_verdict_enqueue)

    started = await queue_mod.maybe_start_qa_stage(session, "task-1-0")

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

    monkeypatch.setattr(queue_mod, "enqueue_qa_worker_job", fail_verdict_enqueue)

    started = await queue_mod.maybe_start_qa_stage(session, "task-2-0")

    assert started is True
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_stage_completes_when_no_qa_eligible_trials(monkeypatch):
    """run_analysis=True but nothing to classify -> complete, do NOT enqueue.

    Every live trial is excluded from QA (bulk-migrated Sauron imports, or
    baseline-gate skips). Enqueueing here would produce a job that can only
    no-op: run_task_qa_job leaves a non-terminal verdict, QaJobHandler reads
    that back as a retryable failure, and the job burns all its attempts before
    landing FAILED -- for what is not an error.
    """
    trial = SimpleNamespace(task_id="task-3")
    task = SimpleNamespace(
        id="task-3",
        org_id="org-1",
        status=TaskStatus.RUNNING,
        run_analysis=True,
        verdict_status=None,
        finished_at=None,
    )
    session = _StageSession(trial=trial, task=task, pending_count=0, qa_eligible=0)

    async def fail_verdict_enqueue(*_args, **_kwargs):
        raise AssertionError("no QA job when there is nothing to classify")

    monkeypatch.setattr(queue_mod, "enqueue_qa_worker_job", fail_verdict_enqueue)

    started = await queue_mod.maybe_start_qa_stage(session, "task-3-0")

    assert started is True
    assert task.status == TaskStatus.COMPLETED
    # Must NOT be left VERDICT_PENDING/QUEUED with no job to move it.
    assert task.verdict_status is None


@pytest.mark.asyncio
async def test_stage_clears_stale_verdict_status_on_completion(monkeypatch):
    """Completing with nothing QA-eligible must CLEAR a stale verdict_status.

    A task can already carry verdict_status=QUEUED from an earlier
    VERDICT_PENDING pass (e.g. a late-arriving trial bounced it back to
    RUNNING). If it now completes here with no eligible trials, leaving that
    QUEUED behind would end the task COMPLETED while verdict_status still reads
    QUEUED -- an inconsistent terminal state.
    """
    trial = SimpleNamespace(task_id="task-4")
    task = SimpleNamespace(
        id="task-4",
        org_id="org-1",
        status=TaskStatus.RUNNING,
        run_analysis=True,
        verdict_status=VerdictStatus.QUEUED,  # stale from a prior pass
        verdict_error="left over",
        finished_at=None,
    )
    session = _StageSession(trial=trial, task=task, pending_count=0, qa_eligible=0)

    async def fail_verdict_enqueue(*_args, **_kwargs):
        raise AssertionError("no QA job when there is nothing to classify")

    monkeypatch.setattr(queue_mod, "enqueue_qa_worker_job", fail_verdict_enqueue)

    started = await queue_mod.maybe_start_qa_stage(session, "task-4-0")

    assert started is True
    assert task.status == TaskStatus.COMPLETED
    assert task.verdict_status is None
    assert task.verdict_error is None


# ---------------------------------------------------------------------------
# run_task_qa_job: one job classifies all trials, then synthesizes the verdict
# ---------------------------------------------------------------------------


class _ScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _QASession:
    def __init__(self, *, task, trials, worker_status=WorkerJobStatus.RUNNING):
        self._task = task
        self._trials = trials
        if isinstance(worker_status, list):
            self._worker_statuses = worker_status
        else:
            self._worker_statuses = [worker_status]

    async def get(self, _model, _key, **_kwargs):
        return self._task

    async def execute(self, _statement):
        return _ScalarsResult(self._trials)

    async def scalar(self, _statement):
        if len(self._worker_statuses) > 1:
            return self._worker_statuses.pop(0)
        return self._worker_statuses[0]


@pytest.mark.asyncio
async def test_run_task_qa_job_classifies_then_synthesizes(monkeypatch):
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
        "task-9-0": SimpleNamespace(id="task-9-0", analysis_status=None, analysis=None),
        "task-9-1": SimpleNamespace(id="task-9-1", analysis_status=None, analysis=None),
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

    async def fake_classify(trial_id, should_store=None):
        classified.append(trial_id)
        assert should_store is not None
        trials[trial_id].analysis_status = AnalysisStatus.SUCCESS
        trials[trial_id].analysis = {
            "classification": "BAD_FAILURE",
            "subtype": "Flaky",
        }

    captured = {"classifications": None}

    async def fake_compute_verdict(classifications, *_args, **_kwargs):
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

    monkeypatch.setattr(qa_handler, "get_session", fake_get_session)
    monkeypatch.setattr("oddish.core.verdict_sync.get_session", fake_get_session)
    monkeypatch.setattr(
        qa_handler, "_load_live_trials_for_classification", fake_load_live
    )
    monkeypatch.setattr(qa_handler, "classify_trial_and_store", fake_classify)
    monkeypatch.setattr(qa_handler, "synthesize_task_verdict", fake_compute_verdict)

    await qa_handler.run_task_qa_job("task-9", queue_key="verdict")

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
async def test_run_task_qa_job_completes_with_no_live_trials(monkeypatch):
    """Zero QA-eligible live trials must complete the task with a TERMINAL
    verdict_status.

    Regression test: this branch references ``TaskStatus``, and a cherry-pick
    once dropped that import from qa_handler while the branch still used it,
    raising NameError at runtime. QaJobHandler maps verdict_status=SUCCESS ->
    ok() and everything else (including a NameError leaving it unset) ->
    retryable failure, so that NameError would burn every retry and land the
    job FAILED for what is not an error. See the long comment above the
    ``if not live_trials:`` branch in qa_handler.py.
    """
    task = SimpleNamespace(
        id="task-15",
        org_id="org-1",
        status=TaskStatus.VERDICT_PENDING,
        verdict_status=VerdictStatus.QUEUED,
        verdict=None,
        verdict_error=None,
        verdict_started_at=None,
        verdict_finished_at=None,
        finished_at=None,
    )
    session = _QASession(task=task, trials=[])

    @asynccontextmanager
    async def fake_get_session():
        yield session

    async def fake_load_live(_task_id):
        return []

    async def fail_classify(_trial_id, should_store=None):
        raise AssertionError("must not classify when there are no live trials")

    async def fail_compute_verdict(*_args, **_kwargs):
        raise AssertionError("verdict synthesis must not run with no live trials")

    monkeypatch.setattr(qa_handler, "get_session", fake_get_session)
    # Unreachable on the green path (the branch returns early), but a regression
    # that falls through to the shielded write would otherwise hit the real DB
    # from a unit test -- which is exactly what happened in this test's RED run.
    monkeypatch.setattr(
        "oddish.core.verdict_sync.get_session", fake_get_session, raising=False
    )
    monkeypatch.setattr(
        qa_handler, "_load_live_trials_for_classification", fake_load_live
    )
    monkeypatch.setattr(qa_handler, "classify_trial_and_store", fail_classify)
    monkeypatch.setattr(qa_handler, "synthesize_task_verdict", fail_compute_verdict)

    await qa_handler.run_task_qa_job("task-15", queue_key="qa")

    # Both of the next two asserts are load-bearing; neither is redundant.
    # ``_QASession`` is a plain fake with no rollback, so a NameError raised
    # after line 252 of qa_handler leaves verdict_status already SUCCESS on the
    # in-memory object -- the status assert is what actually caught the original
    # regression. The verdict_status assert pins the terminal-status contract
    # against a direct edit.
    assert task.verdict_status == VerdictStatus.SUCCESS
    assert task.verdict is None
    assert task.status == TaskStatus.COMPLETED
    assert task.verdict_finished_at is not None
    assert task.finished_at is not None


@pytest.mark.asyncio
async def test_run_task_qa_job_skips_when_already_terminal(monkeypatch):
    task = SimpleNamespace(
        id="task-10",
        verdict_status=VerdictStatus.SUCCESS,
    )
    session = _QASession(task=task, trials=[])

    @asynccontextmanager
    async def fake_get_session():
        yield session

    async def fail_classify(_trial_id, should_store=None):
        raise AssertionError("must not classify when verdict already terminal")

    monkeypatch.setattr(qa_handler, "get_session", fake_get_session)
    monkeypatch.setattr(qa_handler, "classify_trial_and_store", fail_classify)

    # Should return early without touching classification.
    await qa_handler.run_task_qa_job("task-10", queue_key="verdict")
    assert task.verdict_status == VerdictStatus.SUCCESS


@pytest.mark.asyncio
async def test_run_task_qa_job_ignores_cancelled_worker_job(monkeypatch):
    task = SimpleNamespace(
        id="task-11",
        verdict_status=VerdictStatus.QUEUED,
        verdict=None,
        verdict_error=None,
        verdict_finished_at=None,
        status=TaskStatus.VERDICT_PENDING,
        finished_at=None,
    )
    trial = SimpleNamespace(
        id="task-11-0",
        analysis_status=AnalysisStatus.SUCCESS,
        analysis={"classification": "GOOD_SUCCESS", "subtype": "Clean"},
    )
    session = _QASession(
        task=task, trials=[trial], worker_status=WorkerJobStatus.CANCELLED
    )

    @asynccontextmanager
    async def fake_get_session():
        yield session

    async def fake_load_live(_task_id):
        return [(trial.id, AnalysisStatus.SUCCESS)]

    async def fake_compute_verdict(classifications, *_args, **_kwargs):
        return SimpleNamespace(
            is_good=True,
            confidence="high",
            primary_issue=None,
            reasoning="done",
            recommendations=[],
            task_problem_count=0,
            agent_problem_count=0,
            success_count=1,
            harness_error_count=0,
        )

    monkeypatch.setattr(qa_handler, "get_session", fake_get_session)
    monkeypatch.setattr(
        qa_handler, "_load_live_trials_for_classification", fake_load_live
    )
    monkeypatch.setattr(qa_handler, "synthesize_task_verdict", fake_compute_verdict)

    await qa_handler.run_task_qa_job(
        "task-11", queue_key="qa", worker_job_id="cancelled-job"
    )

    assert task.verdict is None
    assert task.verdict_status == VerdictStatus.QUEUED
    assert task.status == TaskStatus.VERDICT_PENDING


@pytest.mark.asyncio
async def test_run_task_qa_job_ignores_final_cancelled_worker_job(monkeypatch):
    task = SimpleNamespace(
        id="task-12",
        verdict_status=VerdictStatus.QUEUED,
        verdict=None,
        verdict_error=None,
        verdict_started_at=None,
        verdict_finished_at=None,
        status=TaskStatus.VERDICT_PENDING,
        finished_at=None,
    )
    trial = SimpleNamespace(
        id="task-12-0",
        analysis_status=AnalysisStatus.SUCCESS,
        analysis={"classification": "GOOD_SUCCESS", "subtype": "Clean"},
    )
    session = _QASession(
        task=task,
        trials=[trial],
        worker_status=[WorkerJobStatus.RUNNING, WorkerJobStatus.CANCELLED],
    )

    @asynccontextmanager
    async def fake_get_session():
        yield session

    async def fake_load_live(_task_id):
        return [(trial.id, AnalysisStatus.SUCCESS)]

    async def fake_compute_verdict(classifications, *_args, **_kwargs):
        assert classifications
        return SimpleNamespace(
            is_good=True,
            confidence="high",
            primary_issue=None,
            reasoning="done",
            recommendations=[],
            task_problem_count=0,
            agent_problem_count=0,
            success_count=1,
            harness_error_count=0,
        )

    monkeypatch.setattr(qa_handler, "get_session", fake_get_session)
    monkeypatch.setattr(
        qa_handler, "_load_live_trials_for_classification", fake_load_live
    )
    monkeypatch.setattr(qa_handler, "synthesize_task_verdict", fake_compute_verdict)

    await qa_handler.run_task_qa_job(
        "task-12", queue_key="qa", worker_job_id="cancelled-job"
    )

    assert task.verdict is None
    assert task.verdict_status == VerdictStatus.RUNNING
    assert task.status == TaskStatus.VERDICT_PENDING


@pytest.mark.asyncio
async def test_run_task_qa_job_stops_classifying_after_cancel(monkeypatch):
    task = SimpleNamespace(
        id="task-13",
        org_id="org-1",
        status=TaskStatus.VERDICT_PENDING,
        verdict_status=VerdictStatus.QUEUED,
        verdict=None,
        verdict_error=None,
        verdict_started_at=None,
        verdict_finished_at=None,
        finished_at=None,
    )
    trials = {
        "task-13-0": SimpleNamespace(
            id="task-13-0", analysis_status=None, analysis=None
        ),
        "task-13-1": SimpleNamespace(
            id="task-13-1", analysis_status=None, analysis=None
        ),
    }
    session = _QASession(
        task=task,
        trials=list(trials.values()),
        worker_status=[
            WorkerJobStatus.RUNNING,
            WorkerJobStatus.RUNNING,
            WorkerJobStatus.CANCELLED,
        ],
    )

    @asynccontextmanager
    async def fake_get_session():
        yield session

    async def fake_load_live(_task_id):
        return [(trial_id, None) for trial_id in trials]

    classified: list[str] = []

    async def fake_classify(trial_id, should_store=None):
        classified.append(trial_id)

    async def fail_compute_verdict(*_args, **_kwargs):
        raise AssertionError("verdict synthesis should stop after cancellation")

    monkeypatch.setattr(qa_handler, "get_session", fake_get_session)
    monkeypatch.setattr(
        qa_handler, "_load_live_trials_for_classification", fake_load_live
    )
    monkeypatch.setattr(qa_handler, "classify_trial_and_store", fake_classify)
    monkeypatch.setattr(qa_handler, "synthesize_task_verdict", fail_compute_verdict)

    await qa_handler.run_task_qa_job(
        "task-13", queue_key="qa", worker_job_id="cancelled-job"
    )

    assert classified == ["task-13-0"]
    assert task.verdict is None
    assert task.verdict_status == VerdictStatus.RUNNING


@pytest.mark.asyncio
async def test_run_task_qa_job_blocks_inflight_classification_store(monkeypatch):
    task = SimpleNamespace(
        id="task-14",
        org_id="org-1",
        status=TaskStatus.VERDICT_PENDING,
        verdict_status=VerdictStatus.QUEUED,
        verdict=None,
        verdict_error=None,
        verdict_started_at=None,
        verdict_finished_at=None,
        finished_at=None,
    )
    trial = SimpleNamespace(id="task-14-0", analysis_status=None, analysis=None)
    session = _QASession(
        task=task,
        trials=[trial],
        worker_status=[
            WorkerJobStatus.RUNNING,
            WorkerJobStatus.RUNNING,
            WorkerJobStatus.CANCELLED,
            WorkerJobStatus.CANCELLED,
        ],
    )

    @asynccontextmanager
    async def fake_get_session():
        yield session

    async def fake_load_live(_task_id):
        return [(trial.id, None)]

    async def fake_classify(_trial_id, should_store=None):
        assert should_store is not None
        if await should_store(session):
            trial.analysis_status = AnalysisStatus.SUCCESS
            trial.analysis = {"classification": "GOOD_SUCCESS"}

    async def fail_compute_verdict(*_args, **_kwargs):
        raise AssertionError("verdict synthesis should stop after cancellation")

    monkeypatch.setattr(qa_handler, "get_session", fake_get_session)
    monkeypatch.setattr(
        qa_handler, "_load_live_trials_for_classification", fake_load_live
    )
    monkeypatch.setattr(qa_handler, "classify_trial_and_store", fake_classify)
    monkeypatch.setattr(qa_handler, "synthesize_task_verdict", fail_compute_verdict)

    await qa_handler.run_task_qa_job(
        "task-14", queue_key="qa", worker_job_id="cancelled-job"
    )

    assert trial.analysis is None
    assert trial.analysis_status is None
    assert task.verdict is None


@pytest.mark.asyncio
async def test_run_task_qa_job_threads_synthesis_args_and_stores_output(monkeypatch):
    """``baseline``/``quality_check_passed``/``timeout`` are threaded into
    verdict synthesis by run_task_qa_job, not re-derived downstream, and the
    synthesized output must reach the stored payload via
    ``build_verdict_payload``."""
    task = SimpleNamespace(
        id="task-20",
        org_id="org-1",
        status=TaskStatus.VERDICT_PENDING,
        verdict_status=VerdictStatus.QUEUED,
        verdict=None,
        verdict_error=None,
        verdict_started_at=None,
        verdict_finished_at=None,
        finished_at=None,
    )
    trial = SimpleNamespace(
        id="task-20-0",
        analysis_status=AnalysisStatus.SUCCESS,
        analysis={"classification": "GOOD_SUCCESS", "subtype": "Clean"},
    )
    session = _QASession(task=task, trials=[trial])

    @asynccontextmanager
    async def fake_get_session():
        yield session

    async def fake_load_live(_task_id):
        return [(trial.id, AnalysisStatus.SUCCESS)]

    captured: dict = {}

    async def stub_verdict_synth(
        classifications, baseline, quality_check_passed, timeout
    ):
        captured["classifications"] = classifications
        captured["baseline"] = baseline
        captured["quality_check_passed"] = quality_check_passed
        captured["timeout"] = timeout
        return SimpleNamespace(
            is_good=True,
            confidence="stub-confidence",
            primary_issue="stub issue",
            reasoning="stub reasoning",
            recommendations=["stub rec"],
        )

    monkeypatch.setattr(qa_handler, "get_session", fake_get_session)
    monkeypatch.setattr("oddish.core.verdict_sync.get_session", fake_get_session)
    monkeypatch.setattr(
        qa_handler, "_load_live_trials_for_classification", fake_load_live
    )
    monkeypatch.setattr(qa_handler, "synthesize_task_verdict", stub_verdict_synth)

    await qa_handler.run_task_qa_job("task-20", queue_key="qa")

    assert len(captured["classifications"]) == 1
    # Threaded through from run_task_qa_job, not re-derived by the stub.
    assert captured["baseline"] is None
    assert captured["quality_check_passed"] is True
    assert captured["timeout"] == 180

    # The stub's output reaches the stored payload via build_verdict_payload /
    # sync_verdict_to_task.
    assert task.verdict_status == VerdictStatus.SUCCESS
    assert task.status == TaskStatus.COMPLETED
    assert task.verdict["is_good"] is True
    assert task.verdict["primary_issue"] == "stub issue"
    assert task.verdict["reasoning"] == "stub reasoning"
    assert task.verdict["recommendations"] == ["stub rec"]


def test_verdict_job_uses_verdict_kind():
    assert WorkerJobKind.QA.value == "QA"

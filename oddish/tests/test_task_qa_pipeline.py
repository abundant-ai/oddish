"""Tests for the unified task-level QA pipeline.

Trajectory analysis is one version-pinned task job: when the selected version's
trials finish, one ``QA`` worker job classifies that cohort and synthesizes the
task verdict. These tests cover:

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
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import oddish.queue as queue_mod  # noqa: E402
from oddish.analyze.models import (  # noqa: E402
    ActionItem,
    ActionItemSource,
    ActionTier,
    Dimension,
    ProblemType,
)
from oddish.db import (  # noqa: E402
    AnalysisStatus,
    TaskStatus,
    TrialModel,
    VerdictStatus,
    WorkerJobKind,
    WorkerJobStatus,
)
from oddish.workers.queue import qa_handler  # noqa: E402
from oddish.workers.queue.qa_handler import (  # noqa: E402
    _classifications_from_trials,
    _qa_store_allowed,
    _qa_trial_filters,
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


def test_qa_trial_filters_pin_the_task_version():
    statement = select(TrialModel.id).where(*_qa_trial_filters("task-9", "task-9-v9"))
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "trials.task_id = 'task-9'" in sql
    assert "trials.task_version_id = 'task-9-v9'" in sql


# ---------------------------------------------------------------------------
# Stage transition: enqueue exactly one task-level QA job
# ---------------------------------------------------------------------------


class _ForUpdateResult:
    def __init__(self, task):
        self._task = task

    def scalar_one_or_none(self):
        return self._task


class _StageSession:
    def __init__(self, *, trial, task, scalar_values, qa_trial_ids=()):
        self._trial = trial
        self._task = task
        self._scalar_values = iter(scalar_values)
        self._qa_trial_ids = qa_trial_ids
        self.scalar_statements = []
        self.flushed = 0

    async def get(self, _model, _key):
        return self._trial

    async def execute(self, _statement):
        return _ForUpdateResult(self._task)

    async def scalar(self, _statement):
        self.scalar_statements.append(_statement)
        return next(self._scalar_values)

    async def scalars(self, _statement):
        return self._qa_trial_ids

    async def flush(self):
        self.flushed += 1


@pytest.mark.asyncio
async def test_stage_enqueues_single_qa_job_when_trials_done(monkeypatch):
    trial = SimpleNamespace(task_id="task-1")
    task = SimpleNamespace(
        id="task-1",
        org_id="org-1",
        current_version_id="task-1-v1",
        status=TaskStatus.RUNNING,
        run_analysis=True,
        verdict_status=None,
        finished_at=None,
    )
    session = _StageSession(
        trial=trial,
        task=task,
        scalar_values=(0,),
        qa_trial_ids=("task-1-0",),
    )

    verdict_calls: list[str] = []

    async def fake_verdict_enqueue(_session, *, task_id, org_id, task_version_id=None):
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
        current_version_id="task-2-v1",
        status=TaskStatus.RUNNING,
        run_analysis=False,
        verdict_status=None,
        finished_at=None,
    )
    session = _StageSession(trial=trial, task=task, scalar_values=(0, 0))

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
        current_version_id="task-3-v1",
        status=TaskStatus.RUNNING,
        run_analysis=True,
        verdict_status=None,
        finished_at=None,
    )
    session = _StageSession(trial=trial, task=task, scalar_values=(0, 0))

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
        current_version_id="task-4-v1",
        status=TaskStatus.RUNNING,
        run_analysis=True,
        verdict_status=VerdictStatus.QUEUED,  # stale from a prior pass
        verdict_error="left over",
        finished_at=None,
    )
    session = _StageSession(trial=trial, task=task, scalar_values=(0, 0))

    async def fail_verdict_enqueue(*_args, **_kwargs):
        raise AssertionError("no QA job when there is nothing to classify")

    monkeypatch.setattr(queue_mod, "enqueue_qa_worker_job", fail_verdict_enqueue)

    started = await queue_mod.maybe_start_qa_stage(session, "task-4-0")

    assert started is True
    assert task.status == TaskStatus.COMPLETED
    assert task.verdict_status is None
    assert task.verdict_error is None


@pytest.mark.asyncio
async def test_stage_waits_only_for_current_version_before_starting_qa(monkeypatch):
    trial = SimpleNamespace(task_id="task-5")
    task = SimpleNamespace(
        id="task-5",
        org_id="org-1",
        current_version_id="task-5-v2",
        status=TaskStatus.RUNNING,
        run_analysis=True,
        verdict=None,
        verdict_status=None,
        finished_at=None,
    )
    session = _StageSession(
        trial=trial,
        task=task,
        scalar_values=(0,),
        qa_trial_ids=("task-5-0",),
    )
    calls = []

    async def fake_enqueue(_session, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(queue_mod, "enqueue_qa_worker_job", fake_enqueue)

    assert await queue_mod.maybe_start_qa_stage(session, "task-5-v1-0") is True
    pending_sql = str(
        session.scalar_statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "trials.task_version_id = 'task-5-v2'" in pending_sql
    assert calls[0]["task_version_id"] == "task-5-v2"
    assert task.status == TaskStatus.VERDICT_PENDING


@pytest.mark.asyncio
async def test_stage_completes_after_historical_trials_finish_without_redoing_qa(
    monkeypatch,
):
    trial = SimpleNamespace(task_id="task-6")
    task = SimpleNamespace(
        id="task-6",
        org_id="org-1",
        current_version_id="task-6-v2",
        status=TaskStatus.RUNNING,
        run_analysis=True,
        verdict={
            "task_version_id": "task-6-v2",
            "trial_ids": ["task-6-0"],
            "is_good": True,
        },
        verdict_status=VerdictStatus.SUCCESS,
        finished_at=None,
    )

    async def fail_enqueue(*_args, **_kwargs):
        raise AssertionError("current-version QA must not be duplicated")

    monkeypatch.setattr(queue_mod, "enqueue_qa_worker_job", fail_enqueue)

    still_running = _StageSession(
        trial=trial,
        task=task,
        scalar_values=(0, 1),
        qa_trial_ids=("task-6-0",),
    )
    assert await queue_mod.maybe_start_qa_stage(still_running, "task-6-v1-0") is False
    assert task.status == TaskStatus.RUNNING

    all_done = _StageSession(
        trial=trial,
        task=task,
        scalar_values=(0, 0),
        qa_trial_ids=("task-6-0",),
    )
    assert await queue_mod.maybe_start_qa_stage(all_done, "task-6-v1-1") is True
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_stage_replaces_legacy_unscoped_verdict_with_version_pinned_qa(
    monkeypatch,
):
    trial = SimpleNamespace(task_id="task-7")
    task = SimpleNamespace(
        id="task-7",
        org_id="org-1",
        current_version_id="task-7-v2",
        status=TaskStatus.RUNNING,
        run_analysis=True,
        # Legacy verdicts predate task_version_id provenance and therefore
        # cannot prove anything about the exact selected-version cohort.
        verdict={"is_good": True},
        verdict_status=VerdictStatus.SUCCESS,
        finished_at=None,
    )
    session = _StageSession(
        trial=trial,
        task=task,
        scalar_values=(0,),
        qa_trial_ids=("task-7-0",),
    )
    calls = []

    async def fake_enqueue(_session, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(queue_mod, "enqueue_qa_worker_job", fake_enqueue)

    assert await queue_mod.maybe_start_qa_stage(session, "task-7-v1-0") is True
    assert calls[0]["task_version_id"] == "task-7-v2"
    assert task.status == TaskStatus.VERDICT_PENDING
    assert task.verdict_status == VerdictStatus.QUEUED


@pytest.mark.asyncio
async def test_stage_reruns_qa_when_current_cohort_changed(monkeypatch):
    trial = SimpleNamespace(task_id="task-8")
    task = SimpleNamespace(
        org_id="org-1",
        current_version_id="task-8-v2",
        status=TaskStatus.RUNNING,
        run_analysis=True,
        verdict={
            "task_version_id": "task-8-v2",
            "trial_ids": ["task-8-0"],
            "is_good": True,
        },
        verdict_status=VerdictStatus.SUCCESS,
    )
    session = _StageSession(
        trial=trial,
        task=task,
        scalar_values=(0,),
        qa_trial_ids=("task-8-0", "task-8-1"),
    )
    calls = []

    async def fake_enqueue(_session, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(queue_mod, "enqueue_qa_worker_job", fake_enqueue)

    assert await queue_mod.maybe_start_qa_stage(session, "task-8-1") is True
    assert calls[0]["task_version_id"] == "task-8-v2"
    assert task.status == TaskStatus.VERDICT_PENDING
    assert task.verdict is None
    assert task.verdict_status == VerdictStatus.QUEUED


@pytest.mark.asyncio
async def test_stage_repairs_missing_current_version_before_qa(monkeypatch):
    trial = SimpleNamespace(task_id="task-9", task_version_id="task-9-v1")
    task = SimpleNamespace(
        org_id="org-1",
        current_version_id=None,
        status=TaskStatus.RUNNING,
        run_analysis=True,
        verdict_status=None,
    )
    session = _StageSession(
        trial=trial,
        task=task,
        # Latest version lookup, then current-version pending count.
        scalar_values=("task-9-v1", 0),
        qa_trial_ids=("task-9-0",),
    )
    calls = []

    async def fake_enqueue(_session, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(queue_mod, "enqueue_qa_worker_job", fake_enqueue)

    assert await queue_mod.maybe_start_qa_stage(session, "task-9-0") is True
    assert task.current_version_id == "task-9-v1"
    assert calls[0]["task_version_id"] == "task-9-v1"


@pytest.mark.asyncio
async def test_stage_finishes_cancelled_qa_after_historical_trials_stop(monkeypatch):
    trial = SimpleNamespace(task_id="task-10")
    task = SimpleNamespace(
        current_version_id="task-10-v2",
        status=TaskStatus.RUNNING,
        run_analysis=True,
        verdict={
            "task_version_id": "task-10-v2",
            "trial_ids": ["task-10-0"],
        },
        verdict_status=VerdictStatus.FAILED,
        verdict_error=queue_mod.USER_CANCELLED_MESSAGE,
    )

    async def fail_enqueue(*_args, **_kwargs):
        raise AssertionError("cancelled QA must not be restarted automatically")

    monkeypatch.setattr(queue_mod, "enqueue_qa_worker_job", fail_enqueue)

    still_running = _StageSession(
        trial=trial,
        task=task,
        scalar_values=(0, 1),
        qa_trial_ids=("task-10-0",),
    )
    assert await queue_mod.maybe_start_qa_stage(still_running, "task-10-v1-0") is False
    assert task.status == TaskStatus.RUNNING

    all_done = _StageSession(
        trial=trial,
        task=task,
        scalar_values=(0, 0),
        qa_trial_ids=("task-10-0",),
    )
    assert await queue_mod.maybe_start_qa_stage(all_done, "task-10-v1-1") is True
    assert task.status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_stage_does_not_requeue_failed_qa_while_history_runs(monkeypatch):
    trial = SimpleNamespace(task_id="task-11")
    task = SimpleNamespace(
        current_version_id="task-11-v2",
        status=TaskStatus.RUNNING,
        run_analysis=True,
        verdict={
            "task_version_id": "task-11-v2",
            "trial_ids": ["task-11-0"],
        },
        verdict_status=VerdictStatus.FAILED,
        verdict_error="synthesis failed",
    )

    async def fail_enqueue(*_args, **_kwargs):
        raise AssertionError("failed QA must require an explicit rerun")

    monkeypatch.setattr(queue_mod, "enqueue_qa_worker_job", fail_enqueue)

    session = _StageSession(
        trial=trial,
        task=task,
        scalar_values=(0, 1),
        qa_trial_ids=("task-11-0",),
    )
    assert await queue_mod.maybe_start_qa_stage(session, "task-11-v1-0") is False
    assert task.status == TaskStatus.RUNNING


@pytest.mark.asyncio
async def test_stage_requeues_failed_qa_from_stale_version(monkeypatch):
    trial = SimpleNamespace(task_id="task-11-stale")
    task = SimpleNamespace(
        org_id="org-1",
        current_version_id="task-11-stale-v2",
        status=TaskStatus.RUNNING,
        run_analysis=True,
        verdict={
            "task_version_id": "task-11-stale-v1",
            "trial_ids": ["task-11-stale-0"],
        },
        verdict_status=VerdictStatus.FAILED,
        verdict_error="old synthesis failed",
    )
    calls = []

    async def fake_enqueue(_session, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(queue_mod, "enqueue_qa_worker_job", fake_enqueue)
    session = _StageSession(
        trial=trial,
        task=task,
        scalar_values=(0,),
        qa_trial_ids=("task-11-stale-1",),
    )

    assert await queue_mod.maybe_start_qa_stage(session, "task-11-stale-v2-0") is True
    assert calls[0]["task_version_id"] == "task-11-stale-v2"
    assert task.verdict_status == VerdictStatus.QUEUED
    assert task.verdict is None


@pytest.mark.asyncio
async def test_legacy_analyzing_waits_only_for_current_version(monkeypatch):
    trial = SimpleNamespace(task_id="task-12")
    task = SimpleNamespace(
        org_id="org-1",
        current_version_id="task-12-v2",
        status=TaskStatus.ANALYZING,
    )
    session = _StageSession(trial=trial, task=task, scalar_values=(0,))
    calls = []

    async def fake_enqueue(_session, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(queue_mod, "enqueue_qa_worker_job", fake_enqueue)

    assert (
        await queue_mod.maybe_advance_legacy_analyzing_task(session, "task-12-v1-0")
        is True
    )
    pending_sql = str(
        session.scalar_statements[0].compile(compile_kwargs={"literal_binds": True})
    )
    assert "trials.task_version_id = 'task-12-v2'" in pending_sql
    assert calls[0]["task_version_id"] == "task-12-v2"


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
    def __init__(
        self,
        *,
        task,
        trials,
        worker_status=WorkerJobStatus.RUNNING,
        task_version=None,
    ):
        self._task = task
        self._trials = trials
        self._task_version = task_version
        if isinstance(worker_status, list):
            self._worker_statuses = worker_status
        else:
            self._worker_statuses = [worker_status]

    async def get(self, model, _key, **_kwargs):
        if getattr(model, "__name__", None) == "TaskVersionModel":
            return self._task_version
        return self._task

    async def execute(self, _statement):
        return _ScalarsResult(self._trials)

    async def scalar(self, _statement):
        entity = _statement.column_descriptions[0].get("entity")
        if entity is TrialModel:
            return 0
        if len(self._worker_statuses) > 1:
            return self._worker_statuses.pop(0)
        return self._worker_statuses[0]


@pytest.mark.asyncio
async def test_qa_store_rejects_a_changed_trial_cohort():
    task = SimpleNamespace(current_version_id="task-9-v9")
    trials = [
        SimpleNamespace(id="task-9-0", analysis_status=AnalysisStatus.SUCCESS),
        SimpleNamespace(id="task-9-1", analysis_status=AnalysisStatus.SUCCESS),
    ]
    session = _QASession(task=task, trials=trials)

    assert (
        await _qa_store_allowed(
            session,
            None,
            "task-9",
            "task-9-v9",
            {"task-9-0"},
        )
        is False
    )


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

    async def fake_load_live(_task_id, _task_version_id=None):
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
    assert task.verdict["trial_ids"] == [
        "task-9-0",
        "task-9-1",
        "task-9-2",
        "task-9-3",
    ]


@pytest.mark.asyncio
async def test_run_task_qa_job_default_pre_trial_synth_is_noop(monkeypatch):
    """CRITICAL INVARIANT: with the default (no-op) ``pre_trial_synth_fn``,
    ``run_task_qa_job`` must not touch the pre_trial columns at all, and the
    verdict path must be byte-for-byte unaffected.
    ``sync_pre_trial_to_task_version`` and ``_claim_pre_trial_version`` are
    patched to blow up if called -- proving the guards actually skip the
    pre-trial path for the legacy default.
    """
    task = SimpleNamespace(
        id="task-9b",
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
        id="task-9b-0",
        analysis_status=AnalysisStatus.SUCCESS,
        analysis={"classification": "GOOD_SUCCESS", "subtype": "Clean"},
    )
    session = _QASession(task=task, trials=[trial])

    @asynccontextmanager
    async def fake_get_session():
        yield session

    async def fake_load_live(_task_id, _task_version_id=None):
        return [(trial.id, AnalysisStatus.SUCCESS)]

    async def fake_compute_verdict(classifications, *_args, **_kwargs):
        return SimpleNamespace(
            is_good=True,
            confidence="high",
            primary_issue="",
            reasoning="fine",
            recommendations=[],
            task_problem_count=0,
            agent_problem_count=0,
            success_count=1,
            harness_error_count=0,
        )

    async def fail_sync_pre_trial(*_args, **_kwargs):
        raise AssertionError(
            "sync_pre_trial_to_task_version must not be called for the default "
            "no-op synth"
        )

    async def fail_claim(*_args, **_kwargs):
        raise AssertionError(
            "_claim_pre_trial_version must not be called with the flag off"
        )

    monkeypatch.setattr(qa_handler, "get_session", fake_get_session)
    monkeypatch.setattr("oddish.core.verdict_sync.get_session", fake_get_session)
    monkeypatch.setattr(
        qa_handler, "_load_live_trials_for_classification", fake_load_live
    )
    monkeypatch.setattr(qa_handler, "synthesize_task_verdict", fake_compute_verdict)
    monkeypatch.setattr(
        qa_handler, "sync_pre_trial_to_task_version", fail_sync_pre_trial
    )
    monkeypatch.setattr(qa_handler, "_claim_pre_trial_version", fail_claim)

    # pre_trial_enabled defaults False, so the pre-trial block is skipped
    # entirely -- the guard never claims a version or calls
    # sync_pre_trial_to_task_version.
    await qa_handler.run_task_qa_job("task-9b", queue_key="verdict")

    # pre_trial was never assigned onto the task -- proves the guard skipped
    # persistence entirely rather than writing an empty/None payload.
    assert not hasattr(task, "pre_trial")
    assert not hasattr(task, "pre_trial_status")
    # Verdict path is unaffected.
    assert task.verdict_status == VerdictStatus.SUCCESS
    assert task.status == TaskStatus.COMPLETED


def test_pre_trial_disabled_by_default():
    """Belt-and-braces: the gate that keeps pre-trial a no-op is off by default."""
    from oddish.config import settings

    assert settings.pre_trial_enabled is False


@pytest.mark.asyncio
async def test_run_task_qa_job_uses_injected_pre_trial_synth_fn(monkeypatch):
    """An injected ``pre_trial_synth_fn`` runs once before the per-trial loop
    and its result is persisted onto the claimed task *version* via
    ``sync_pre_trial_to_task_version`` -- without completing the task or
    touching verdict fields (or any task column at all)."""
    task = SimpleNamespace(
        id="task-9c",
        org_id="org-1",
        status=TaskStatus.VERDICT_PENDING,
        verdict_status=VerdictStatus.QUEUED,
        verdict=None,
        verdict_error=None,
        verdict_started_at=None,
        verdict_finished_at=None,
        finished_at=None,
    )
    version = SimpleNamespace(
        id="task-9c-v1",
        task_id="task-9c",
        pre_trial=None,
        pre_trial_status=None,
        pre_trial_error=None,
        pre_trial_finished_at=None,
    )
    trial = SimpleNamespace(
        id="task-9c-0",
        analysis_status=AnalysisStatus.SUCCESS,
        analysis={"classification": "GOOD_SUCCESS", "subtype": "Clean"},
    )
    session = _QASession(task=task, trials=[trial], task_version=version)

    @asynccontextmanager
    async def fake_get_session():
        yield session

    async def fake_load_live(_task_id, _task_version_id=None):
        return [(trial.id, AnalysisStatus.SUCCESS)]

    async def fake_compute_verdict(classifications, *_args, **_kwargs):
        return SimpleNamespace(
            is_good=True,
            confidence="high",
            primary_issue="",
            reasoning="fine",
            recommendations=[],
            task_problem_count=0,
            agent_problem_count=0,
            success_count=1,
            harness_error_count=0,
        )

    captured: dict = {}

    async def stub_pre_trial_synth(task_id, task_version_id, trial_ids, timeout):
        captured["task_id"] = task_id
        captured["task_version_id"] = task_version_id
        captured["trial_ids"] = trial_ids
        return [
            ActionItem(
                source=ActionItemSource.PRE_TRIAL,
                problem_type=ProblemType.MISMATCH,
                dimension=Dimension.ORACLE,
                file="solution.py",
                line_start=1,
                line_end=1,
                title="t",
                detail="d",
                recommendation="r",
                tier=ActionTier.SHOULD_FIX,
            )
        ]

    async def fake_claim(_task_id, _task_version_id):
        return version.id

    async def fake_store_allowed(_session, _worker_job_id, _task_id, _version_id):
        return True

    monkeypatch.setattr(qa_handler, "get_session", fake_get_session)
    monkeypatch.setattr("oddish.core.verdict_sync.get_session", fake_get_session)
    monkeypatch.setattr(
        qa_handler, "_load_live_trials_for_classification", fake_load_live
    )
    monkeypatch.setattr(qa_handler, "synthesize_task_verdict", fake_compute_verdict)
    monkeypatch.setattr(qa_handler.settings, "pre_trial_enabled", True)
    monkeypatch.setattr(qa_handler, "_pre_trial_synth_fn", stub_pre_trial_synth)
    monkeypatch.setattr(qa_handler, "_claim_pre_trial_version", fake_claim)
    monkeypatch.setattr(qa_handler, "_pre_trial_store_allowed", fake_store_allowed)

    await qa_handler.run_task_qa_job("task-9c", queue_key="verdict")

    assert captured["task_id"] == "task-9c"
    assert captured["task_version_id"] == "task-9c-v1"
    assert captured["trial_ids"] == ["task-9c-0"]
    assert version.pre_trial_status == VerdictStatus.SUCCESS
    assert version.pre_trial["items"][0]["file"] == "solution.py"
    # Pre-trial writes land on the version, never on the task.
    assert not hasattr(task, "pre_trial")
    assert not hasattr(task, "pre_trial_status")
    # Pre-trial must never complete the task or touch verdict fields.
    assert task.status == TaskStatus.COMPLETED  # completed by verdict, not pre-trial
    assert task.verdict_status == VerdictStatus.SUCCESS


@pytest.mark.asyncio
async def test_run_task_qa_job_pre_trial_failure_never_blocks_verdict(monkeypatch):
    """A raising ``pre_trial_synth_fn`` must be swallowed: recorded as a
    pre_trial failure, but the verdict path still runs to completion."""
    task = SimpleNamespace(
        id="task-9d",
        org_id="org-1",
        status=TaskStatus.VERDICT_PENDING,
        verdict_status=VerdictStatus.QUEUED,
        verdict=None,
        verdict_error=None,
        verdict_started_at=None,
        verdict_finished_at=None,
        finished_at=None,
    )
    version = SimpleNamespace(
        id="task-9d-v1",
        task_id="task-9d",
        pre_trial=None,
        pre_trial_status=None,
        pre_trial_error=None,
        pre_trial_finished_at=None,
    )
    trial = SimpleNamespace(
        id="task-9d-0",
        analysis_status=AnalysisStatus.SUCCESS,
        analysis={"classification": "GOOD_SUCCESS", "subtype": "Clean"},
    )
    session = _QASession(task=task, trials=[trial], task_version=version)

    @asynccontextmanager
    async def fake_get_session():
        yield session

    async def fake_load_live(_task_id, _task_version_id=None):
        return [(trial.id, AnalysisStatus.SUCCESS)]

    async def fake_compute_verdict(classifications, *_args, **_kwargs):
        return SimpleNamespace(
            is_good=True,
            confidence="high",
            primary_issue="",
            reasoning="fine",
            recommendations=[],
            task_problem_count=0,
            agent_problem_count=0,
            success_count=1,
            harness_error_count=0,
        )

    async def boom_pre_trial_synth(task_id, task_version_id, trial_ids, timeout):
        raise RuntimeError("sandbox exploded")

    async def fake_claim(_task_id, _task_version_id):
        return version.id

    async def fake_store_allowed(_session, _worker_job_id, _task_id, _version_id):
        return True

    monkeypatch.setattr(qa_handler, "get_session", fake_get_session)
    monkeypatch.setattr("oddish.core.verdict_sync.get_session", fake_get_session)
    monkeypatch.setattr(
        qa_handler, "_load_live_trials_for_classification", fake_load_live
    )
    monkeypatch.setattr(qa_handler, "synthesize_task_verdict", fake_compute_verdict)
    monkeypatch.setattr(qa_handler.settings, "pre_trial_enabled", True)
    monkeypatch.setattr(qa_handler, "_pre_trial_synth_fn", boom_pre_trial_synth)
    monkeypatch.setattr(qa_handler, "_claim_pre_trial_version", fake_claim)
    monkeypatch.setattr(qa_handler, "_pre_trial_store_allowed", fake_store_allowed)

    await qa_handler.run_task_qa_job("task-9d", queue_key="verdict")

    assert version.pre_trial_status == VerdictStatus.FAILED
    assert "sandbox exploded" in version.pre_trial_error
    # Verdict path is unaffected by the pre-trial crash.
    assert task.verdict_status == VerdictStatus.SUCCESS
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_run_task_qa_job_releases_claim_when_store_vetoed(monkeypatch):
    """When the store gate vetoes persistence (job cancelled mid-audit, or a
    new upload made the audited snapshot stale), the RUNNING claim must be
    released -- not left to wait out the lease -- and the verdict path must
    be unaffected."""
    task = SimpleNamespace(
        id="task-9e",
        org_id="org-1",
        status=TaskStatus.VERDICT_PENDING,
        verdict_status=VerdictStatus.QUEUED,
        verdict=None,
        verdict_error=None,
        verdict_started_at=None,
        verdict_finished_at=None,
        finished_at=None,
    )
    version = SimpleNamespace(
        id="task-9e-v1",
        task_id="task-9e",
        pre_trial=None,
        pre_trial_status=VerdictStatus.RUNNING,  # as _claim_pre_trial_version left it
        pre_trial_started_at=object(),
        pre_trial_error=None,
        pre_trial_finished_at=None,
    )
    trial = SimpleNamespace(
        id="task-9e-0",
        analysis_status=AnalysisStatus.SUCCESS,
        analysis={"classification": "GOOD_SUCCESS", "subtype": "Clean"},
    )
    session = _QASession(task=task, trials=[trial], task_version=version)

    @asynccontextmanager
    async def fake_get_session():
        yield session

    async def fake_load_live(_task_id, _task_version_id=None):
        return [(trial.id, AnalysisStatus.SUCCESS)]

    async def fake_compute_verdict(classifications, *_args, **_kwargs):
        return SimpleNamespace(
            is_good=True,
            confidence="high",
            primary_issue="",
            reasoning="fine",
            recommendations=[],
            task_problem_count=0,
            agent_problem_count=0,
            success_count=1,
            harness_error_count=0,
        )

    async def stub_pre_trial_synth(task_id, task_version_id, trial_ids, timeout):
        return []

    async def fake_claim(_task_id, _task_version_id):
        return version.id

    async def veto_store(_session, _worker_job_id, _task_id, _version_id):
        return False

    monkeypatch.setattr(qa_handler, "get_session", fake_get_session)
    monkeypatch.setattr("oddish.core.verdict_sync.get_session", fake_get_session)
    monkeypatch.setattr(
        qa_handler, "_load_live_trials_for_classification", fake_load_live
    )
    monkeypatch.setattr(qa_handler, "synthesize_task_verdict", fake_compute_verdict)
    monkeypatch.setattr(qa_handler.settings, "pre_trial_enabled", True)
    monkeypatch.setattr(qa_handler, "_pre_trial_synth_fn", stub_pre_trial_synth)
    monkeypatch.setattr(qa_handler, "_claim_pre_trial_version", fake_claim)
    monkeypatch.setattr(qa_handler, "_pre_trial_store_allowed", veto_store)

    await qa_handler.run_task_qa_job("task-9e", queue_key="verdict")

    # Claim released: unclaimed again, nothing persisted.
    assert version.pre_trial_status is None
    assert version.pre_trial_started_at is None
    assert version.pre_trial is None
    assert version.pre_trial_finished_at is None
    # Verdict path is unaffected.
    assert task.verdict_status == VerdictStatus.SUCCESS
    assert task.status == TaskStatus.COMPLETED


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

    async def fake_load_live(_task_id, _task_version_id=None):
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

    async def fake_load_live(_task_id, _task_version_id=None):
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

    async def fake_load_live(_task_id, _task_version_id=None):
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

    async def fake_load_live(_task_id, _task_version_id=None):
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

    async def fake_load_live(_task_id, _task_version_id=None):
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

    async def fake_load_live(_task_id, _task_version_id=None):
        return [(trial.id, AnalysisStatus.SUCCESS)]

    captured: dict = {}

    async def stub_verdict_synth(
        classifications, baseline, quality_check_passed, timeout, task_id=None
    ):
        captured["classifications"] = classifications
        captured["baseline"] = baseline
        captured["quality_check_passed"] = quality_check_passed
        captured["timeout"] = timeout
        captured["task_id"] = task_id
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
    assert captured["task_id"] == "task-20"
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

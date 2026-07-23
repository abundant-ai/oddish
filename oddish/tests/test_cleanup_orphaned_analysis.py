"""Trials must not stay stranded in a non-terminal ``analysis_status``.

The task-level QA job marks one trial's analysis RUNNING at a time; a worker
killed mid-classification (or a cancelled job's skipped store) used to leave
that trial non-terminal forever -- an incident accumulated 4k+ of these,
rendering as phantom "running" analyses on the dashboard. Two layers now heal
them, both covered here:

* the stale-heartbeat QA mirror resets the dead job's task trials inline
  (RETRYING -> QUEUED, FAILED -> FAILED);
* the ``_reset_orphaned_trial_analysis`` sweep phase is the backstop for
  every other leak path.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import ORPHANED_ANALYSIS_ERROR_PREFIX  # noqa: E402
from oddish.core.baseline_gate import GATE_SKIP_PREFIX  # noqa: E402
from oddish.db import VerdictStatus  # noqa: E402
from oddish.queue import requeue_inflight_trial_analysis  # noqa: E402
from oddish.workers.queue import cleanup  # noqa: E402


class _Result:
    def __init__(
        self, *, rows: list[Any] | None = None, rowcount: int = 0, scalar: Any = None
    ) -> None:
        self._rows = rows or []
        self.rowcount = rowcount
        self._scalar = scalar

    def mappings(self) -> "_Result":
        return self

    def all(self) -> list[Any]:
        return self._rows

    def scalar_one_or_none(self) -> Any:
        return self._scalar


class _RecordingSession:
    """Sniffs executed SQL; dispatch is keyed off statement text fragments."""

    def __init__(self, *, task: Any | None = None) -> None:
        self.task = task
        self.executed: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, statement, params: dict[str, Any] | None = None):
        sql = " ".join(str(statement).split())
        self.executed.append((sql, params or {}))
        if "FROM tasks" in sql and "FOR UPDATE" in sql:
            return _Result(scalar=self.task)
        if "UPDATE trials" in sql and "analysis_status = 'FAILED'" in sql:
            return _Result(rowcount=3)
        if "UPDATE trials" in sql and "analysis_status = 'QUEUED'" in sql:
            return _Result(rowcount=2)
        return _Result()

    async def scalar(self, *args: Any, **kwargs: Any) -> Any:
        return None

    async def flush(self) -> None:
        return None

    def updates(self, fragment: str) -> list[tuple[str, dict[str, Any]]]:
        return [(sql, params) for sql, params in self.executed if fragment in sql]


# ---------------------------------------------------------------------------
# Sweep phase
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orphan_sweep_runs_both_arms_and_returns_counts() -> None:
    session = _RecordingSession()

    failed, requeued = await cleanup._reset_orphaned_trial_analysis(session)

    assert (failed, requeued) == (3, 2)

    fail_arm = session.updates("analysis_status = 'FAILED'")
    assert len(fail_arm) == 1
    fail_sql, fail_params = fail_arm[0]
    # Never-classifiable rows only: superseded / skipped / gate-skipped /
    # imported trials, a soft-deleted task, or a terminal task with no active
    # QA job.
    assert "superseded_by_trial_id IS NOT NULL" in fail_sql
    assert "tr.status = 'SKIPPED'" in fail_sql
    assert "imported_at IS NOT NULL" in fail_sql
    assert "t.status IN ('COMPLETED', 'FAILED')" in fail_sql
    assert "'QUEUED', 'RETRYING'" in fail_sql  # active-QA-job guard
    assert fail_params["stale_minutes"] == cleanup.ORPHANED_ANALYSIS_MINUTES
    assert fail_params["batch_limit"] == cleanup.ORPHANED_ANALYSIS_BATCH_LIMIT
    assert fail_params["reason"] == cleanup.ORPHANED_ANALYSIS_REASON
    assert fail_params["reason"].startswith(ORPHANED_ANALYSIS_ERROR_PREFIX)
    assert fail_params["gate_skip_pattern"] == f"{GATE_SKIP_PREFIX}%"

    requeue_arm = session.updates("analysis_status = 'QUEUED'")
    assert len(requeue_arm) == 1
    requeue_sql, requeue_params = requeue_arm[0]
    # Re-queueable rows: live trials of a non-terminal task with no QA job
    # currently RUNNING (a RUNNING job legitimately holds one trial RUNNING).
    assert "tr.analysis_status = 'RUNNING'" in requeue_sql
    assert "superseded_by_trial_id IS NULL" in requeue_sql
    assert "NOT LIKE :gate_skip_pattern" in requeue_sql
    assert "t.status NOT IN ('COMPLETED', 'FAILED')" in requeue_sql
    assert "wj.status::text = 'RUNNING'" in requeue_sql
    assert requeue_params["stale_minutes"] == cleanup.ORPHANED_ANALYSIS_MINUTES

    # Both arms must be soft-delete aware (raw SQL bypasses the ORM filter)
    # and must never WAIT on a trial row lock: the sweep transaction may hold
    # task locks, and blocking on trials inverts the trials-then-task lock
    # order cancel_tasks_runs documents (deadlock). Lock trials rows only.
    assert "tr.deleted_at IS NULL" in fail_sql
    assert "tr.deleted_at IS NULL" in requeue_sql
    assert "FOR UPDATE OF tr SKIP LOCKED" in fail_sql
    assert "FOR UPDATE OF tr SKIP LOCKED" in requeue_sql


# ---------------------------------------------------------------------------
# QA stale-heartbeat mirror
# ---------------------------------------------------------------------------


def _qa_row(new_status: str) -> dict[str, Any]:
    return {
        "id": "wj-qa-1",
        "kind": "QA",
        "new_status": new_status,
        "subject_table": "tasks",
        "subject_id": "task-1",
        "attempts": 3,
        "max_attempts": 3,
        "error_message": "Worker heartbeat stalled for over 15 minutes.",
        "payload": {"task_version_id": "task-1-v1"},
    }


@pytest.mark.asyncio
async def test_qa_reap_retrying_requeues_running_trial_analyses() -> None:
    task = SimpleNamespace(id="task-1", verdict_status=None, verdict_error=None)
    session = _RecordingSession(task=task)

    result = await cleanup._mirror_stale_job_to_domain_row(session, _qa_row("RETRYING"))

    assert result is None
    assert task.verdict_status == VerdictStatus.QUEUED
    requeues = session.updates("analysis_status = 'QUEUED'")
    assert len(requeues) == 1
    sql, params = requeues[0]
    assert "analysis_status = 'RUNNING'" in sql
    assert "deleted_at IS NULL" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert params["task_id"] == "task-1"


@pytest.mark.asyncio
async def test_qa_reap_failed_finalizes_nonterminal_trial_analyses() -> None:
    task = SimpleNamespace(
        id="task-1",
        verdict_status=None,
        verdict_error=None,
        verdict_finished_at=None,
    )
    session = _RecordingSession(task=task)

    await cleanup._mirror_stale_job_to_domain_row(session, _qa_row("FAILED"))

    assert task.verdict_status == VerdictStatus.FAILED
    fails = session.updates("analysis_status = 'FAILED'")
    assert len(fails) == 1
    sql, params = fails[0]
    assert "analysis_status IN ('PENDING', 'QUEUED', 'RUNNING')" in sql
    assert "deleted_at IS NULL" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert params["task_id"] == "task-1"
    assert params["task_version_id"] == "task-1-v1"
    # The reason carries the orphaned-analysis sentinel so a later resurrect
    # (append) reopens these rows instead of inheriting a permanent gap.
    assert params["reason"].startswith(ORPHANED_ANALYSIS_ERROR_PREFIX)
    assert "heartbeat stalled" in params["reason"]


# ---------------------------------------------------------------------------
# Shared requeue helper (append-supersede cancel + QA-mirror RETRYING arm)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requeue_helper_reopens_inflight_and_orphaned_rows() -> None:
    session = _RecordingSession()

    await requeue_inflight_trial_analysis(
        session,
        task_id="task-9",
        task_version_id="task-9-v3",
    )

    requeues = session.updates("analysis_status = 'QUEUED'")
    assert len(requeues) == 1
    sql, params = requeues[0]
    # Reopens both in-flight rows and orphan-finalized FAILED rows...
    assert "analysis_status = 'RUNNING'" in sql
    assert "analysis_status = 'FAILED'" in sql
    assert "analysis_error LIKE :orphan_pattern" in sql
    # ... clearing the stale error/finish markers so the row reads as queued.
    assert "analysis_error = NULL" in sql
    assert "analysis_finished_at = NULL" in sql
    # Never-classifiable rows stay finalized (no oscillation with the sweep).
    assert "superseded_by_trial_id IS NULL" in sql
    assert "task_version_id IS NOT DISTINCT FROM :task_version_id" in sql
    assert "imported_at IS NULL" in sql
    assert "status <> 'SKIPPED'" in sql
    assert "NOT LIKE :gate_skip_pattern" in sql
    assert params["task_version_id"] == "task-9-v3"
    # Soft-delete aware, and never waits on a contended trial row.
    assert "deleted_at IS NULL" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert params == {
        "task_id": "task-9",
        "task_version_id": "task-9-v3",
        "gate_skip_pattern": f"{GATE_SKIP_PREFIX}%",
        "orphan_pattern": f"{ORPHANED_ANALYSIS_ERROR_PREFIX}%",
    }

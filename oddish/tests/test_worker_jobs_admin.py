from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from oddish.core.admin import get_worker_jobs_admin_core
from oddish.db import ACTIVE_WORKER_JOB_KINDS


NOW = datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc)


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _Session:
    def __init__(self, *results):
        self.results = iter(results)
        self.calls = []

    async def execute(self, statement, params):
        self.calls.append((str(statement), params))
        return _Result(next(self.results))


def test_worker_jobs_snapshot_counts_effective_kinds_and_enriches_rows():
    summary_rows = [
        SimpleNamespace(
            kind="agent",
            running=4,
            ready=2,
            scheduled=1,
            blocked=1,
            stale=1,
            failed_last_hour=2,
        ),
        SimpleNamespace(
            kind="qa",
            running=1,
            ready=0,
            scheduled=0,
            blocked=0,
            stale=0,
            failed_last_hour=0,
        ),
        SimpleNamespace(
            kind="qa_eval",
            running=1,
            ready=0,
            scheduled=0,
            blocked=0,
            stale=0,
            failed_last_hour=0,
        ),
    ]
    job_rows = [
        SimpleNamespace(
            id="job-1",
            kind="qa",
            status="RUNNING",
            queue_key="anthropic/claude-sonnet-4-6",
            trial_id="task-1-9",
            task_id="task-1",
            task_name="Fix retry accounting",
            experiment_id="experiment-1",
            experiment_name="retry sweep",
            agent="claude-code",
            model="claude-sonnet-4-6",
            harbor_stage="executing_agent",
            attempts=1,
            max_attempts=3,
            created_at=NOW,
            available_after=NOW,
            claimed_at=NOW,
            heartbeat_at=NOW,
            finished_at=None,
            admission_reason=None,
            error_message=None,
            heartbeat_failure_count=0,
            last_heartbeat_error=None,
            current_worker_id="worker-1",
            current_queue_slot=7,
            is_stale=False,
            total_jobs=3,
        )
    ]
    pipeline_rows = [
        SimpleNamespace(
            task_id="task-stuck",
            task_name="Stuck verdict",
            status="VERDICT_PENDING",
            run_analysis=True,
            verdict_status=None,
            issue="active_task_without_active_trials",
            updated_at=NOW,
            total_count=2,
        )
    ]
    session = _Session(summary_rows, job_rows, pipeline_rows)

    response = asyncio.run(
        get_worker_jobs_admin_core(
            session,
            org_id="org-1",
            stale_after_minutes=10,
            sample_limit=1,
        )
    )

    assert response.summary.running == 6
    assert response.summary.ready == 2
    assert response.summary.scheduled == 1
    assert response.summary.blocked == 1
    assert response.summary.stale == 1
    assert response.summary.failed_last_hour == 2
    assert [row.kind for row in response.summary.by_kind] == [
        "agent",
        "qa",
        "qa_eval",
    ]
    assert response.jobs[0].task_name == "Fix retry accounting"
    assert response.jobs[0].experiment_name == "retry sweep"
    assert response.jobs[0].harbor_stage == "executing_agent"
    assert response.pipeline_issue_count == 2
    assert response.pipeline_issues[0].task_name == "Stuck verdict"
    assert response.total_jobs == 3
    assert response.truncated is True

    active_kinds = [kind.value for kind in ACTIVE_WORKER_JOB_KINDS]
    for statement, params in session.calls[:2]:
        assert "wj.kind::text = ANY" in statement
        assert "tr.kind" in statement
        assert "'BLOCKED'" in statement
        assert "'CANCELLED'" not in statement
        assert params["active_kinds"] == active_kinds
        assert params["org_id"] == "org-1"


def test_worker_jobs_snapshot_handles_an_idle_queue():
    session = _Session([], [], [])

    response = asyncio.run(get_worker_jobs_admin_core(session, org_id="org-1"))

    assert response.summary.running == 0
    assert response.summary.ready == 0
    assert response.summary.by_kind == []
    assert response.jobs == []
    assert response.pipeline_issue_count == 0
    assert response.pipeline_issues == []
    assert response.total_jobs == 0
    assert response.truncated is False


@pytest.mark.parametrize(
    ("sample", "expected_filter"),
    [
        (
            "active",
            "wj.status::text IN ('QUEUED', 'RETRYING', 'RUNNING', 'BLOCKED')",
        ),
        ("attention", "OR wj.status::text = 'BLOCKED'"),
        ("failures", "wj.status::text = 'FAILED'"),
    ],
)
def test_worker_jobs_filters_the_requested_sample_before_limit(sample, expected_filter):
    session = _Session([], [], [])

    asyncio.run(get_worker_jobs_admin_core(session, org_id="org-1", sample=sample))

    sample_query = session.calls[1][0]
    sample_where = sample_query.split("WHERE wj.kind::text", 1)[1].split("ORDER BY", 1)[
        0
    ]
    assert expected_filter in sample_where
    assert sample_query.index(expected_filter) < sample_query.index(
        "LIMIT :sample_limit"
    )
    if sample == "failures":
        assert "'BLOCKED'" not in sample_where
        assert "heartbeat_at" not in sample_where

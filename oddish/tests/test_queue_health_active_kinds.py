from __future__ import annotations

import asyncio
from types import SimpleNamespace

from oddish.core.admin import get_queue_health_core, get_queue_status_core
from oddish.db import ACTIVE_WORKER_JOB_KINDS


class _Result:
    def __init__(self, rows=()):
        self.rows = rows

    def all(self):
        return self.rows


class _Session:
    def __init__(self, rows=()):
        self.calls = []
        self.rows = rows

    async def execute(self, statement, params):
        self.calls.append((str(statement), params))
        return _Result(self.rows)


def test_queue_capacity_only_counts_active_worker_job_kinds():
    session = _Session()

    response = asyncio.run(get_queue_health_core(session, include_global_details=False))

    active_kinds = [kind.value for kind in ACTIVE_WORKER_JOB_KINDS]
    assert response.totals_queued == 0
    assert response.totals_running == 0
    assert len(session.calls) == 3
    for statement, params in session.calls[1:]:
        assert "kind::text = ANY" in statement
        assert params["active_kinds"] == active_kinds


def test_queue_status_only_counts_active_worker_job_kinds():
    session = _Session()

    response = asyncio.run(get_queue_status_core(session))

    assert response.queues == []
    assert len(session.calls) == 1
    statement, params = session.calls[0]
    assert "wj.kind::text = ANY" in statement
    assert params["active_kinds"] == [kind.value for kind in ACTIVE_WORKER_JOB_KINDS]


def test_queue_status_keeps_qa_evals_out_of_agent_trial_totals():
    row = SimpleNamespace(
        kind="QA_EVAL",
        queue_key="anthropic/claude-sonnet-4-6",
        queued=2,
        running=1,
    )
    response = asyncio.run(get_queue_status_core(_Session([row])))

    assert response.trial_queues == []
    assert response.analysis_queued == 2
    assert response.analysis_running == 1
    assert response.queues[0].kind == "QA_EVAL"

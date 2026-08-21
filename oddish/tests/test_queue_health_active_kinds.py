from __future__ import annotations

import asyncio

from oddish.core.admin import get_queue_health_core
from oddish.db import ACTIVE_WORKER_JOB_KINDS


class _EmptyResult:
    def all(self):
        return []


class _Session:
    def __init__(self):
        self.calls = []

    async def execute(self, statement, params):
        self.calls.append((str(statement), params))
        return _EmptyResult()


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

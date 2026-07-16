"""Analyzer jobs must outrank the QA backlog they share a queue with.

ANALYZER and QA both land on ``get_qa_queue_key()``, and the claim orders by
``priority DESC, running_count ASC, created_at ASC``. Every enqueue site leaves
priority at 0, so the first two keys tie and claims fall through to pure FIFO --
an analyzer enqueued behind a QA burst waits for the whole backlog to drain.

Uses a fake session: enqueue_worker_job needs only add/flush, and
worker_jobs.subject_id is not a foreign key, so no live DB is required.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.db.models import WorkerJobKind  # noqa: E402
from oddish.queue import (  # noqa: E402
    enqueue_analyzer_worker_job,
    enqueue_qa_worker_job,
)


class _FakeSession:
    """Captures rows handed to add(); enqueue_worker_job needs only add/flush."""

    def __init__(self) -> None:
        self.added: list = []

    def add(self, row) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None


@pytest.mark.asyncio
async def test_analyzer_job_is_enqueued_above_default_priority():
    session = _FakeSession()

    row = await enqueue_analyzer_worker_job(
        session, analyzer_id="deadbeef", org_id="org123"
    )

    assert row.kind == WorkerJobKind.ANALYZER
    assert row.priority == 1


@pytest.mark.asyncio
async def test_analyzer_outranks_qa_on_the_shared_queue():
    session = _FakeSession()

    analyzer = await enqueue_analyzer_worker_job(
        session, analyzer_id="deadbeef", org_id="org123"
    )
    qa = await enqueue_qa_worker_job(session, task_id="task1", org_id="org123")

    # Same queue_key, so the claim's ORDER BY priority DESC is what decides:
    # without this the two tie at 0 and the older QA backlog always wins.
    assert analyzer.queue_key == qa.queue_key
    assert analyzer.priority > qa.priority

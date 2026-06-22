"""harbor_variant_id threads from the trial pin onto worker_jobs (dispatch key)."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.db import WorkerJobModel, get_session  # noqa: E402
from oddish.db.models import WorkerJobKind  # noqa: E402
from oddish.workers.jobs.enqueue import (  # noqa: E402
    EnqueueRequest,
    bulk_enqueue_worker_jobs,
    enqueue_worker_job,
)

_RUN = uuid.uuid4().hex[:8]


class _FakeSession:
    def __init__(self) -> None:
        self.added: list = []
        self.flushed = False

    def add(self, row) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        self.flushed = True


def test_enqueue_request_defaults_to_default_variant():
    req = EnqueueRequest(kind=WorkerJobKind.TRIAL, queue_key="q")
    assert req.harbor_variant_id == "default"


@pytest.mark.asyncio
async def test_enqueue_worker_job_sets_harbor_variant_id():
    session = _FakeSession()
    row = await enqueue_worker_job(
        session,
        EnqueueRequest(
            kind=WorkerJobKind.TRIAL,
            queue_key="q",
            harbor_variant_id="ephemeral",
            payload={"trial_id": "t"},
        ),
        validate=False,
    )
    assert row.harbor_variant_id == "ephemeral"


@pytest.mark.asyncio
async def test_bulk_enqueue_persists_harbor_variant_id():
    queue_key = f"variant-test-{_RUN}"
    async with get_session() as session:
        ids = await bulk_enqueue_worker_jobs(
            session,
            [
                EnqueueRequest(
                    kind=WorkerJobKind.TRIAL,
                    queue_key=queue_key,
                    harbor_variant_id="ephemeral",
                    payload={"trial_id": "t1"},
                ),
                EnqueueRequest(
                    kind=WorkerJobKind.TRIAL,
                    queue_key=queue_key,
                    payload={"trial_id": "t2"},  # default variant
                ),
            ],
            validate=False,
        )
        try:
            rows = (
                await session.execute(
                    select(
                        WorkerJobModel.id, WorkerJobModel.harbor_variant_id
                    ).where(WorkerJobModel.id.in_(ids))
                )
            ).all()
            variants = {r.id: r.harbor_variant_id for r in rows}
            assert variants[ids[0]] == "ephemeral"
            assert variants[ids[1]] == "default"
        finally:
            await session.execute(
                WorkerJobModel.__table__.delete().where(WorkerJobModel.id.in_(ids))
            )

"""A worker only claims worker_jobs rows of the variant it was spawned for."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.db import WorkerJobModel, WorkerJobStatus, get_session  # noqa: E402
from oddish.db.models import WorkerJobKind  # noqa: E402
from oddish.workers.jobs.enqueue import EnqueueRequest, bulk_enqueue_worker_jobs  # noqa: E402
from oddish.workers.queue.worker_job_single_job import claim_single_worker_job  # noqa: E402

_RUN = uuid.uuid4().hex[:8]


@pytest.mark.asyncio
async def test_claim_is_scoped_to_harbor_variant():
    queue_key = f"variant-claim-{_RUN}"
    async with get_session() as session:
        ids = await bulk_enqueue_worker_jobs(
            session,
            [
                EnqueueRequest(
                    kind=WorkerJobKind.TRIAL,
                    queue_key=queue_key,
                    harbor_variant_id="default",
                    payload={"trial_id": "d1"},
                ),
                EnqueueRequest(
                    kind=WorkerJobKind.TRIAL,
                    queue_key=queue_key,
                    harbor_variant_id="ephemeral",
                    payload={"trial_id": "e1"},
                ),
            ],
            validate=False,
        )
        await session.commit()

    try:
        # An ephemeral worker must claim the ephemeral row, never the default one.
        claimed = await claim_single_worker_job(
            queue_key,
            worker_id="w-eph",
            queue_slot=0,
            harbor_variant_id="ephemeral",
        )
        assert claimed is not None
        assert claimed.harbor_variant_id == "ephemeral"
        assert claimed.payload["trial_id"] == "e1"

        # The default row is untouched and still claimable by a default worker.
        async with get_session() as session:
            statuses = dict(
                (
                    await session.execute(
                        select(
                            WorkerJobModel.id, WorkerJobModel.status
                        ).where(WorkerJobModel.id.in_(ids))
                    )
                ).all()
            )
        assert statuses[ids[0]] == WorkerJobStatus.QUEUED  # default still queued
        assert statuses[ids[1]] == WorkerJobStatus.RUNNING  # ephemeral claimed

        default_claim = await claim_single_worker_job(
            queue_key,
            worker_id="w-def",
            queue_slot=1,
            harbor_variant_id="default",
        )
        assert default_claim is not None
        assert default_claim.harbor_variant_id == "default"
        assert default_claim.payload["trial_id"] == "d1"

        # A third variant has no work -> nothing claimable.
        none_claim = await claim_single_worker_job(
            queue_key,
            worker_id="w-x",
            queue_slot=2,
            harbor_variant_id="harbor-next",
        )
        assert none_claim is None
    finally:
        async with get_session() as session:
            await session.execute(
                WorkerJobModel.__table__.delete().where(WorkerJobModel.id.in_(ids))
            )
            await session.commit()

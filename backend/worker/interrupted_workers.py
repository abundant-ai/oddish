"""Modal-specific liveness evidence; core recovery never depends on Modal APIs."""

import asyncio
import logging
from datetime import datetime, timezone

from modal.client import _Client
from modal_proto import api_pb2
from sqlalchemy import text

from oddish.db import get_session
from oddish.workers.queue.worker_job_single_job import settle_interrupted_worker
from oddish.workers.queue.cleanup import finish_interrupted_attempt_cleanup

logger = logging.getLogger(__name__)


async def container_stopped_at(container_id: str) -> datetime | None:
    """TaskGetInfo is the same exact-container check used by Modal's stop CLI.

    Absence from TaskList or a failed lookup never proves a container stopped.
    """
    try:
        client = await _Client.from_env()
        response = await client.stub.TaskGetInfo(
            api_pb2.TaskGetInfoRequest(task_id=container_id),
            timeout=10,
        )
        if response.info.finished_at > 0:
            return datetime.fromtimestamp(response.info.finished_at, timezone.utc)
    except Exception:
        logger.warning(
            "Modal container status unknown: %s", container_id, exc_info=True
        )
    return None


async def recover_interrupted_workers(
    *,
    function_call_id: str | None = None,
    reservation_token: str | None = None,
    replacement_container_id: str | None = None,
) -> int:
    if reservation_token is not None and (
        not function_call_id or not replacement_container_id
    ):
        return 0
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    text("""
            SELECT ra.worker_job_id, ra.attempt, ra.worker_id, ra.modal_container_id
            FROM worker_resource_attempts ra JOIN worker_jobs wj
              ON wj.id = ra.worker_job_id AND wj.attempts = ra.attempt
             AND wj.current_worker_id = ra.worker_id
            WHERE wj.status::text = 'RUNNING' AND ra.outcome IS NULL
              AND ra.modal_container_id IS NOT NULL
              AND (CAST(:call AS text) IS NULL OR ra.modal_function_call_id = :call)
              AND (CAST(:token AS text) IS NULL OR ra.reservation_token = :token)
              AND (CAST(:replacement AS text) IS NULL OR ra.modal_container_id <> :replacement)
            ORDER BY wj.heartbeat_at NULLS FIRST LIMIT 100
        """),
                    dict(
                        call=function_call_id,
                        token=reservation_token,
                        replacement=replacement_container_id,
                    ),
                )
            )
            .mappings()
            .all()
        )
    # Bound control-plane requests; a slow lookup must not starve the existing
    # heartbeat sweep. No database session is held while awaiting Modal.
    semaphore = asyncio.Semaphore(10)

    async def check(row):
        async with semaphore:
            return await container_stopped_at(row["modal_container_id"])

    stopped = await asyncio.gather(*(check(row) for row in rows))
    recovered = 0
    for row, stopped_at in zip(rows, stopped):
        if stopped_at is None:
            continue
        recovered += await settle_interrupted_worker(
            job_id=row["worker_job_id"],
            attempt=row["attempt"],
            worker_id=row["worker_id"],
            container_id=row["modal_container_id"],
            stopped_at=stopped_at,
        )
    await finish_interrupted_attempt_cleanup()
    return recovered

from __future__ import annotations

import json
from datetime import timedelta
from typing import Awaitable, Callable

from pgqueuer import QueueManager  # type: ignore[attr-defined]
from pgqueuer.db import AsyncpgPoolDriver
from pgqueuer.models import Job

from oddish.config import settings
from oddish.db import get_pool
from oddish.workers.queue.analysis_handler import run_analysis_job
from oddish.workers.queue.shared import console
from oddish.workers.queue.trial_handler import run_trial_job
from oddish.workers.queue.verdict_handler import run_verdict_job


async def create_queue_manager_base() -> QueueManager:
    """Create a QueueManager with a shared DB driver."""
    pool = await get_pool()
    driver = AsyncpgPoolDriver(pool)
    return QueueManager(driver)


def register_queue_entrypoints(
    qm: QueueManager,
    *,
    queue_keys: tuple[str, ...],
    concurrency_limits: dict[str, int],
    retry_timer: timedelta,
    handler: Callable[[Job, str], Awaitable[None]],
) -> None:
    """Register queue entrypoints with shared handler logic."""
    for queue_key in queue_keys:
        if queue_key not in concurrency_limits:
            raise ValueError(f"Missing concurrency limit for queue key: {queue_key}")
        limit = concurrency_limits[queue_key]

        @qm.entrypoint(
            queue_key,
            concurrency_limit=limit,
            retry_timer=retry_timer,
        )
        async def entrypoint(
            job: Job,
            _queue_key: str = queue_key,
        ) -> None:
            console.print(f"[dim]{_queue_key} entrypoint received job {job.id}[/dim]")
            await handler(job, _queue_key)

async def _discover_active_queue_keys() -> tuple[str, ...]:
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT DISTINCT entrypoint
        FROM pgqueuer
        WHERE status IN ('queued', 'picked')
        """
    )
    discovered: set[str] = set()
    for row in rows:
        entrypoint = row.get("entrypoint")
        if not entrypoint:
            continue
        raw_key = str(entrypoint).strip().lower().replace(" ", "_")
        if not raw_key:
            continue
        # Keep raw keys so legacy queued jobs still drain, and add canonical
        # keys so new writes converge to a single entrypoint.
        discovered.add(raw_key)
        discovered.add(settings.normalize_queue_key(raw_key))
    discovered.update(settings.get_known_queue_keys())
    if not discovered:
        discovered = {"default"}
    return tuple(sorted(discovered))


async def create_queue_manager() -> QueueManager:
    """Create and configure the QueueManager with model-keyed entrypoints."""
    # Lazy import to avoid circular dependency (api.py imports from workers)
    from oddish.api import get_queue_concurrency

    qm = await create_queue_manager_base()

    # All job types route through provider queues
    # Each provider handles: trials, analysis (if claude), verdict (if claude)
    retry_timer = timedelta(minutes=settings.trial_retry_timer_minutes)

    queue_keys = await _discover_active_queue_keys()
    concurrency_limits = {
        queue_key: get_queue_concurrency(queue_key) for queue_key in queue_keys
    }

    console.print("[green]Creating QueueManager with queue concurrency limits:[/green]")
    for queue_key in queue_keys:
        console.print(f"  {queue_key}: {concurrency_limits[queue_key]}")

    async def handle_provider_job(job: Job, queue_key: str) -> None:
        """Route job to appropriate handler based on job_type in payload."""
        payload = json.loads(job.payload.decode())
        job_type = payload["job_type"]

        console.print(
            f"[dim]Queue {queue_key} handling job_type={job_type} job_id={job.id}[/dim]"
        )

        if job_type == "trial":
            await run_trial_job(job, queue_key=queue_key)
        elif job_type == "analysis":
            await run_analysis_job(job, queue_key=queue_key)
        elif job_type == "verdict":
            await run_verdict_job(job, queue_key=queue_key)
        else:
            console.print(f"[red]Unknown job_type: {job_type}[/red]")

    register_queue_entrypoints(
        qm,
        queue_keys=queue_keys,
        concurrency_limits=concurrency_limits,
        retry_timer=retry_timer,
        handler=handle_provider_job,
    )

    return qm

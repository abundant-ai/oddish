import json
from datetime import timedelta

import anyio
import asyncpg
from pgqueuer import buffers, helpers
from pgqueuer.db import AsyncpgDriver
from pgqueuer.models import Context, Job
from pgqueuer.qm import QueueManager

from oddish.config import settings
from oddish.workers.queue import (
    register_queue_entrypoints,
    run_analysis_job,
    run_trial_job,
    run_verdict_job,
)

from .github import (
    notify_github_analysis,
    notify_github_trial,
    notify_github_verdict,
)
from .runtime import console, enforce_trial_environment


async def create_single_job_queue_manager(
    queue_key: str,
    *,
    worker_id: str,
    queue_slot: int,
) -> tuple[QueueManager, asyncpg.Connection]:
    """
    Create QueueManager configured to process exactly ONE job then exit.

    Each Modal worker container runs this to claim and process a single job.
    PGQueuer's SKIP LOCKED ensures no duplicate processing across workers.
    """
    # Use a dedicated connection for the one-shot worker. This avoids
    # LISTEN/NOTIFY listener state inside an asyncpg pool and keeps connection
    # usage predictable on Supabase/Modal.
    connection = await asyncpg.connect(
        settings.asyncpg_url,
        statement_cache_size=0,
    )
    qm = QueueManager(AsyncpgDriver(connection))

    retry_timer = timedelta(minutes=settings.trial_retry_timer_minutes)

    async def handle_queue_job(job: Job, queue_key: str) -> None:
        raw = job.payload.decode(errors="replace")
        try:
            payload = json.loads(raw)
        except Exception as e:
            console.print(
                f"[red]Invalid job payload JSON (queue_key={queue_key}, job_id={job.id}): {e}[/red]"
            )
            console.print(f"[dim]Raw payload: {raw!r}[/dim]")
            raise

        job_type = payload.get("job_type")

        # Backwards-compat: older payloads may not include job_type, but do include trial_id.
        if job_type is None and payload.get("trial_id"):
            job_type = "trial"
            console.print(
                f"[yellow]Job missing job_type; treating as trial (queue_key={queue_key}, job_id={job.id})[/yellow]"
            )

        console.print(
            f"[cyan]Processing job_type={job_type} job_id={job.id} (queue_key={queue_key})[/cyan]"
        )

        try:
            if job_type == "trial":
                if not payload.get("trial_id"):
                    raise ValueError(
                        f"Trial job missing trial_id (queue_key={queue_key}, job_id={job.id})"
                    )
                trial_id = payload["trial_id"]
                await enforce_trial_environment(trial_id)
                await run_trial_job(
                    job,
                    queue_key=queue_key,
                    worker_id=worker_id,
                    queue_slot=queue_slot,
                )
                await notify_github_trial(trial_id)

            elif job_type == "analysis":
                if not payload.get("trial_id"):
                    raise ValueError(
                        f"Analysis job missing trial_id (queue_key={queue_key}, job_id={job.id})"
                    )
                trial_id = payload["trial_id"]
                await run_analysis_job(job, queue_key=queue_key)
                await notify_github_analysis(trial_id)

            elif job_type == "verdict":
                if not payload.get("task_id"):
                    raise ValueError(
                        f"Verdict job missing task_id (queue_key={queue_key}, job_id={job.id})"
                    )
                task_id = payload["task_id"]
                await run_verdict_job(job, queue_key=queue_key)
                await notify_github_verdict(task_id)

            else:
                raise ValueError(
                    f"Unknown job_type={job_type!r} (queue_key={queue_key}, job_id={job.id})"
                )
        finally:
            # Signal shutdown after processing one job.
            console.print(f"[green]Job {job.id} complete, signaling shutdown[/green]")
            qm.shutdown.set()

    register_queue_entrypoints(
        qm,
        queue_keys=(queue_key,),
        concurrency_limits={queue_key: 1},
        retry_timer=retry_timer,
        handler=handle_queue_job,
    )

    return qm, connection


async def run_single_job_without_listener(qm: QueueManager) -> bool:
    """Dequeue and dispatch at most one job without starting pgqueuer listeners."""
    await qm.verify_structure()
    heartbeat_buffer_timeout = helpers.retry_timer_buffer_timeout(
        [x.parameters.retry_timer for x in qm.entrypoint_registry.values()]
    )

    async with (
        buffers.JobStatusLogBuffer(
            max_size=1,
            callback=qm.queries.log_jobs,
        ) as jbuff,
        buffers.HeartbeatBuffer(
            max_size=1,
            timeout=heartbeat_buffer_timeout / 4,
            callback=qm.queries.update_heartbeat,
        ) as hbuff,
        buffers.RequestsPerSecondBuffer(
            max_size=1,
            callback=qm.update_rps_stats,
        ) as rpsbuff,
        qm.connection,
    ):
        async for job in qm.fetch_jobs(batch_size=1, global_concurrency_limit=1):
            await rpsbuff.add(job.entrypoint)
            qm.job_context[job.id] = Context(
                cancellation=anyio.CancelScope(),
                resources=qm.resources,
            )
            await qm._dispatch(job, jbuff, hbuff)
            return True

    return False

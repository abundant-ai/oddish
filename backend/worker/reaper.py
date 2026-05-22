"""Scheduled reaper. Respawns stranded worker_jobs and reaps stale-
heartbeat RUNNING rows. In previews, the ``preview_epoch_at`` row in
``oddish_runtime_config`` (written by ``cancel_cloned_preview_work``)
gates the respawn pass so cloned-from-prod work can't be resurrected.
"""

from __future__ import annotations

import asyncio

import modal

from observability import span as _otel_span

from modal_app import REAPER_PERIOD_SECONDS, app, image, runtime_secrets
from oddish.db import close_database_connections, get_pool
from oddish.workers.queue.cleanup import cleanup_orphaned_queue_state

from .runtime import console


@app.function(
    image=image,
    secrets=runtime_secrets,
    timeout=300,
    schedule=modal.Period(seconds=REAPER_PERIOD_SECONDS),
    max_containers=1,
)
async def reaper() -> dict:
    from .functions import get_wrapper_for_queue_key

    with _otel_span("worker.reaper"):
        try:
            try:
                cleanup_counts = await cleanup_orphaned_queue_state()
                if any(cleanup_counts.values()):
                    console.print(
                        "metric=reaper_orphan_cleanup "
                        + " ".join(f"{k}={v}" for k, v in cleanup_counts.items())
                    )
            except Exception as exc:
                console.print(
                    f"[yellow]reaper orphan cleanup failed: {exc!r}[/yellow]"
                )

            pool = await get_pool()
            rows = await pool.fetch(
                """
                SELECT queue_key
                FROM   worker_jobs
                WHERE  status::text IN ('QUEUED', 'RETRYING')
                  AND  available_after <= NOW()
                  AND  created_at > COALESCE(
                          (SELECT value::timestamptz
                           FROM   oddish_runtime_config
                           WHERE  key = 'preview_epoch_at'),
                          '1970-01-01'::timestamptz
                       )
                """
            )
            queue_keys = [str(r["queue_key"]) for r in rows]
            if not queue_keys:
                return {"respawned": 0}

            tasks = [
                get_wrapper_for_queue_key(qk).spawn.aio(queue_key=qk)
                for qk in queue_keys
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            errors = [repr(r) for r in results if isinstance(r, BaseException)]
            if errors:
                console.print(f"[red]reaper spawn errors: {errors[:3]}[/red]")
            console.print(
                f"[green]reaper: respawned {len(queue_keys) - len(errors)} / "
                f"{len(queue_keys)} rows[/green]"
            )
            return {"respawned": len(queue_keys) - len(errors), "errors": errors[:5]}
        finally:
            try:
                await close_database_connections()
            except Exception:
                pass

"""Scheduled reaper: re-spawn worker_jobs rows that aren't actually running.

Two failure modes the reaper handles:

1. Enqueue inserted a row but the dispatcher spawn was lost (Modal
   API hiccup, deploy churn, etc.). Row sits QUEUED forever.
2. A worker claimed a row (RUNNING) but the container died without
   updating status. Stale-heartbeat sweep covers this; we only
   re-spawn QUEUED/RETRYING rows here.

Runs on a slow schedule (default every 5 min). It is the ONLY
periodic component in the new architecture; everything else is
event-driven via notify_dispatch.
"""

from __future__ import annotations

import asyncio

import modal

from observability import span as _otel_span

from modal_app import REAPER_PERIOD_SECONDS, app, image, runtime_secrets
from oddish.db import close_database_connections, get_pool

from .runtime import console


@app.function(
    image=image,
    secrets=runtime_secrets,
    timeout=300,
    schedule=modal.Period(seconds=REAPER_PERIOD_SECONDS),
    max_containers=1,
)
async def reaper() -> dict:
    """Re-spawn the wrapper for any QUEUED/RETRYING row whose
    ``available_after`` is in the past and that isn't already RUNNING."""
    from .functions import get_wrapper_for_queue_key

    with _otel_span("worker.reaper"):
        try:
            pool = await get_pool()
            rows = await pool.fetch(
                """
                SELECT queue_key
                FROM   worker_jobs
                WHERE  status::text IN ('QUEUED', 'RETRYING')
                  AND  available_after <= NOW()
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
                f"{len(queue_keys)} queued rows[/green]"
            )
            return {"respawned": len(queue_keys) - len(errors), "errors": errors[:5]}
        finally:
            try:
                await close_database_connections()
            except Exception:
                pass

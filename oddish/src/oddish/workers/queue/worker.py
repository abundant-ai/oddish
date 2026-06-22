from __future__ import annotations

import asyncio
import os
from functools import partial
import signal

from oddish.config import settings
from oddish.db import close_pool
from oddish.workers.harbor_runner import log_local_storage_snapshot
from oddish.workers.queue.queue_manager import (
    run_assigned_queue_worker,
    run_polling_worker,
)
from oddish.workers.queue.shared import console

# When set, the worker drains exactly this one queue_key and exits -- the
# one-job-per-container model a Docker/Kubernetes Dispatcher.spawn launches.
# Unset -> long-poll every active queue_key (the standalone-server pool).
ASSIGNED_QUEUE_KEY_ENV = "ODDISH_WORKER_QUEUE_KEY"


async def run_worker() -> None:
    """Run the queue worker."""
    console.print("[green]Starting Oddish queue worker...[/green]")
    log_local_storage_snapshot(settings.harbor_jobs_dir)

    def _announce_shutdown(received_sig: signal.Signals) -> None:
        console.print(
            f"[yellow]Received {received_sig.name}, shutting down...[/yellow]"
        )

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, partial(_announce_shutdown, sig))

    assigned_queue_key = os.environ.get(ASSIGNED_QUEUE_KEY_ENV)
    try:
        if assigned_queue_key:
            console.print(
                f"[cyan]Draining assigned queue_key={assigned_queue_key}[/cyan]"
            )
            await run_assigned_queue_worker(assigned_queue_key)
        else:
            await run_polling_worker()
    except asyncio.CancelledError:
        console.print("[yellow]Worker cancelled[/yellow]")
    finally:
        await close_pool()
        console.print("[green]Worker shutdown complete[/green]")


if __name__ == "__main__":
    asyncio.run(run_worker())

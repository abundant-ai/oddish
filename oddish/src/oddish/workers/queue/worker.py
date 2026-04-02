from __future__ import annotations

import asyncio
import signal

from oddish.db import close_pool
from oddish.workers.queue.queue_manager import run_polling_worker
from oddish.workers.queue.shared import console


async def run_worker() -> None:
    """Run the queue worker."""
    console.print("[green]Starting Oddish queue worker...[/green]")

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(
            sig,
            lambda s=sig: console.print(
                f"[yellow]Received {s.name}, shutting down...[/yellow]"
            ),
        )

    try:
        await run_polling_worker()
    except asyncio.CancelledError:
        console.print("[yellow]Worker cancelled[/yellow]")
    finally:
        await close_pool()
        console.print("[green]Worker shutdown complete[/green]")


if __name__ == "__main__":
    asyncio.run(run_worker())

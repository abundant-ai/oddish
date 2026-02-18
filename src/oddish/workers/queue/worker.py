from __future__ import annotations

import asyncio
import signal

from oddish.config import settings
from oddish.db import close_pool
from oddish.workers.queue.queue_manager import create_queue_manager
from oddish.workers.queue.shared import console


async def run_worker() -> None:
    """Run the PGQueuer worker."""
    console.print("[green]Starting Oddish PGQueuer worker...[/green]")

    qm = await create_queue_manager()

    # Log registered entrypoints
    entrypoints = [
        "claude (trials + analysis + verdict)",
        "gemini (trials)",
        "openai (trials - includes codex, gpt)",
        "default (trials - oracle, etc)",
    ]
    console.print("[blue]Registered provider queues:[/blue]")
    for ep in entrypoints:
        console.print(f"  - {ep}")
    console.print(
        f"[dim]Provider concurrency: {settings.default_provider_concurrency}[/dim]"
    )

    # Handle shutdown gracefully
    shutdown_event = asyncio.Event()

    def handle_signal(sig):
        console.print(f"[yellow]Received {sig.name}, shutting down...[/yellow]")
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(
            sig,
            lambda s=sig: handle_signal(s),  # type: ignore[misc]
        )

    try:
        # Run the queue manager
        await qm.run()
    except asyncio.CancelledError:
        console.print("[yellow]Worker cancelled[/yellow]")
    finally:
        await close_pool()
        console.print("[green]Worker shutdown complete[/green]")


# CLI entry point
if __name__ == "__main__":
    asyncio.run(run_worker())

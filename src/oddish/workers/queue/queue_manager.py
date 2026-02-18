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


def register_provider_entrypoints(
    qm: QueueManager,
    *,
    providers: tuple[str, ...],
    concurrency_limits: dict[str, int],
    retry_timer: timedelta,
    handler: Callable[[Job, str], Awaitable[None]],
) -> None:
    """Register provider entrypoints with shared handler logic."""
    display_names = {
        "claude": "Claude",
        "gemini": "Gemini",
        "openai": "OpenAI",
        "default": "Default",
    }
    for provider in providers:
        if provider not in concurrency_limits:
            raise ValueError(f"Missing concurrency limit for provider: {provider}")
        limit = concurrency_limits[provider]
        display_name = display_names.get(provider, provider.capitalize())

        @qm.entrypoint(
            provider,
            concurrency_limit=limit,
            retry_timer=retry_timer,
        )
        async def entrypoint(
            job: Job,
            _provider: str = provider,
            _display_name: str = display_name,
        ) -> None:
            console.print(
                f"[dim]{_display_name} entrypoint received job {job.id}[/dim]"
            )
            await handler(job, _provider)


async def create_queue_manager() -> QueueManager:
    """Create and configure the QueueManager with all entrypoints (provider-based)."""
    # Lazy import to avoid circular dependency (api.py imports from workers)
    from oddish.api import get_provider_concurrency

    qm = await create_queue_manager_base()

    # All job types route through provider queues
    # Each provider handles: trials, analysis (if claude), verdict (if claude)
    retry_timer = timedelta(minutes=settings.trial_retry_timer_minutes)

    # Get concurrency limits (from runtime overrides or config defaults)
    claude_concurrency = get_provider_concurrency("claude")
    gemini_concurrency = get_provider_concurrency("gemini")
    openai_concurrency = get_provider_concurrency("openai")
    default_concurrency = get_provider_concurrency("default")

    console.print(
        "[green]Creating QueueManager with provider concurrency limits:[/green]"
    )
    console.print(f"  claude:  {claude_concurrency}")
    console.print(f"  gemini:  {gemini_concurrency}")
    console.print(f"  openai:  {openai_concurrency}")
    console.print(f"  default: {default_concurrency}")

    async def handle_provider_job(job: Job, provider: str) -> None:
        """Route job to appropriate handler based on job_type in payload."""
        payload = json.loads(job.payload.decode())
        job_type = payload["job_type"]

        console.print(
            f"[dim]Provider {provider} handling job_type={job_type} job_id={job.id}[/dim]"
        )

        if job_type == "trial":
            await run_trial_job(job, provider=provider)
        elif job_type == "analysis":
            await run_analysis_job(job, provider=provider)
        elif job_type == "verdict":
            await run_verdict_job(job, provider=provider)
        else:
            console.print(f"[red]Unknown job_type: {job_type}[/red]")

    register_provider_entrypoints(
        qm,
        providers=("claude", "gemini", "openai", "default"),
        concurrency_limits={
            "claude": claude_concurrency,
            "gemini": gemini_concurrency,
            "openai": openai_concurrency,
            "default": default_concurrency,
        },
        retry_timer=retry_timer,
        handler=handle_provider_job,
    )

    return qm

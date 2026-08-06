"""Durable worker execution and the best-effort QA summary seam."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from oddish.core.helpers import _has_fetchable_trajectory
from oddish.db import TrialModel, get_session
from oddish.workers.queue.shared import console
from oddish.workers.queue.worker_job_single_job import heartbeat_worker_job

TRAJECTORY_SUMMARY_TIMEOUT_SECONDS = 300
_HEARTBEAT_INTERVAL_SECONDS = 30

TrajectorySummaryProviderFn = Callable[[str, str | None], Awaitable[dict | None]]

_trajectory_summary_provider: TrajectorySummaryProviderFn | None = None


class TrajectorySummaryUnavailableError(RuntimeError):
    """A missing trial or trajectory makes this job permanently impossible."""


def register_trajectory_summary_provider(fn: TrajectorySummaryProviderFn) -> None:
    """Install the hosted trajectory-summary provider."""
    global _trajectory_summary_provider
    _trajectory_summary_provider = fn


async def _heartbeat_summary_job(worker_job_id: str, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), _HEARTBEAT_INTERVAL_SECONDS)
        except TimeoutError:
            try:
                await heartbeat_worker_job(worker_job_id)
            except Exception as exc:
                console.print(
                    f"[yellow]Trajectory summary heartbeat failed: {exc}[/yellow]"
                )


async def run_trajectory_summary_job(
    trial_id: str,
    *,
    schema_version: str,
    triggered_by_user_id: str | None,
    worker_job_id: str | None = None,
) -> dict:
    """Generate and store one summary, raising retryable failures to the runner."""
    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)
        if trial is None:
            raise TrajectorySummaryUnavailableError(f"Trial {trial_id} not found")
        if not _has_fetchable_trajectory(trial):
            raise TrajectorySummaryUnavailableError(
                f"Trial {trial_id} has no trajectory"
            )

    provider = _trajectory_summary_provider
    if provider is None:
        raise RuntimeError("No trajectory summary provider is registered")

    stop = asyncio.Event()
    heartbeat_task = (
        asyncio.create_task(_heartbeat_summary_job(worker_job_id, stop))
        if worker_job_id
        else None
    )
    try:
        summary = await asyncio.wait_for(
            provider(trial_id, triggered_by_user_id),
            timeout=TRAJECTORY_SUMMARY_TIMEOUT_SECONDS,
        )
    finally:
        if heartbeat_task is not None:
            stop.set()
            await heartbeat_task

    if not summary:
        raise TrajectorySummaryUnavailableError(
            f"Trial {trial_id} no longer has a trajectory"
        )
    if summary.get("schema_version") != schema_version:
        raise RuntimeError(
            f"Trajectory summary schema mismatch for {trial_id}: "
            f"expected {schema_version!r}, got {summary.get('schema_version')!r}"
        )
    return summary


async def ensure_trajectory_summary(trial_id: str) -> dict | None:
    """Best-effort summary generation used only to enrich QA classification."""
    try:
        return await run_trajectory_summary_job(
            trial_id,
            schema_version="5",
            triggered_by_user_id=None,
        )
    except Exception as exc:
        console.print(
            f"[yellow]Trajectory summary unavailable for {trial_id}; "
            f"classifying without it: {exc}[/yellow]"
        )
    return None


async def resolve_trajectory_components(trial_id: str) -> list[dict] | None:
    summary = await ensure_trajectory_summary(trial_id)
    return (summary.get("components") or None) if summary else None

"""Daytona execution backend. ``from daytona import …`` is lazy (confined to
teardown) so importing this module never requires the Daytona SDK."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from oddish.config import settings
from oddish.runtime.ports import Capabilities, ExecutionBackend

logger = logging.getLogger(__name__)


async def reap_stale_daytona_sandboxes(stale_after_minutes: int = 15) -> int:
    from daytona import AsyncDaytona, ListSandboxesQuery, SandboxState

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_after_minutes)
    terminal = {SandboxState.ERROR, SandboxState.BUILD_FAILED}
    client = AsyncDaytona()
    try:
        query = ListSandboxesQuery(
            limit=50,
            labels={"harbor.managed": "true", "oddish.managed": "true"},
            states=list(terminal),
            created_at_before=cutoff,
        )
        sandboxes = []
        async for sandbox in client.list(query):
            sandboxes.append(sandbox)
            if len(sandboxes) == 50:
                break
        semaphore = asyncio.Semaphore(10)

        async def delete(sandbox) -> int:
            try:
                async with semaphore:
                    sandbox = await client.get(sandbox.id, request_timeout=10)
                    value = sandbox.updated_at or sandbox.created_at
                    updated_at = datetime.fromisoformat(value) if value else None
                    if (
                        sandbox.labels.get("harbor.managed") != "true"
                        or sandbox.labels.get("oddish.managed") != "true"
                        or sandbox.state not in terminal
                        or updated_at is None
                        or updated_at.tzinfo is None
                        or updated_at > cutoff
                    ):
                        return 0
                    await client.delete(sandbox, timeout=30)
                    return 1
            except Exception:
                logger.exception(
                    "Failed to delete stale Daytona sandbox %s", sandbox.id
                )
                return 0

        return sum(await asyncio.gather(*(delete(sandbox) for sandbox in sandboxes)))
    finally:
        await client.close()


class DaytonaBackend:
    name = "daytona"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            gpu=None,
            private_registry_pull=False,
            network_egress="allow",
            persistent_volumes=False,
            streaming_logs=True,
            memory_snapshot_fork=False,
            cold_start="seconds",
        )

    def harbor_env_kwargs(self, base_kwargs: dict[str, Any]) -> dict[str, Any]:
        labels = {**(base_kwargs.get("labels") or {}), "oddish.managed": "true"}
        return {
            "auto_stop_interval_mins": settings.daytona_auto_stop_interval_mins,
            "auto_delete_interval_mins": settings.daytona_auto_delete_interval_mins,
            "ephemeral": settings.daytona_ephemeral,
            **base_kwargs,
            "auto_labels": True,
            "labels": labels,
        }

    async def teardown(self, external_id: str) -> bool:
        if not external_id:
            return False
        try:
            from daytona import AsyncDaytona

            client = AsyncDaytona()
            try:
                sandbox = await client.get(external_id)
                await client.delete(sandbox)
            finally:
                await client.close()
        except Exception:
            logger.exception(
                "DaytonaBackend.teardown: failed to terminate %s", external_id
            )
            return False
        logger.info("DaytonaBackend.teardown: terminated %s", external_id)
        return True

    @contextlib.contextmanager
    def capture_diagnostics(self, job_dir: Path) -> Iterator[Path | None]:
        # Daytona has no SDK-output capture; no-op.
        yield None


_: ExecutionBackend = DaytonaBackend()  # structural conformance check at import

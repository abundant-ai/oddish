"""Daytona execution backend. ``from daytona import …`` is lazy (confined to
teardown) so importing this module never requires the Daytona SDK."""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any, Iterator

from oddish.config import settings
from oddish.runtime.ports import Capabilities, ExecutionBackend

logger = logging.getLogger(__name__)


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
        # Mirror the original spread in harbor_runner: provider defaults first,
        # caller kwargs last (caller wins).
        return {
            "auto_stop_interval_mins": settings.daytona_auto_stop_interval_mins,
            "auto_delete_interval_mins": settings.daytona_auto_delete_interval_mins,
            "ephemeral": settings.daytona_ephemeral,
            **base_kwargs,
        }

    async def teardown(self, external_id: str) -> bool:
        # Implemented in Task 5.
        raise NotImplementedError

    @contextlib.contextmanager
    def capture_diagnostics(self, job_dir: Path) -> Iterator[Path | None]:
        # Daytona has no SDK-output capture; no-op.
        yield None


_: ExecutionBackend = DaytonaBackend()  # structural conformance check at import

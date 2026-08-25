from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any, Iterator

from oddish.runtime.ports import Capabilities, ExecutionBackend

logger = logging.getLogger(__name__)


class ArchilBackend:
    name = "archil"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            gpu=None,
            private_registry_pull=False,
            network_egress="allow",
            persistent_volumes=False,
            streaming_logs=False,
            memory_snapshot_fork=True,
            cold_start="seconds",
        )

    def harbor_env_kwargs(self, base_kwargs: dict[str, Any]) -> dict[str, Any]:
        return {"pause_http_proxy": True, **base_kwargs}

    async def teardown(self, external_id: str) -> bool:
        if not external_id:
            return False
        try:
            from archil import Archil

            client = Archil(timeout=120)
            try:
                sandbox = await client.sandboxes.get.aio(external_id)
                if sandbox.status not in {"stopped", "exited", "failed"}:
                    sandbox = await sandbox.stop.aio()
                await sandbox.delete.aio()
            finally:
                await client.close.aio()
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                logger.info("ArchilBackend.teardown: %s already gone", external_id)
                return True
            logger.exception(
                "ArchilBackend.teardown: failed to terminate %s", external_id
            )
            return False
        logger.info("ArchilBackend.teardown: terminated %s", external_id)
        return True

    @contextlib.contextmanager
    def capture_diagnostics(self, job_dir: Path) -> Iterator[Path | None]:
        yield None


_: ExecutionBackend = ArchilBackend()

"""Modal execution backend. ``import modal`` is lazy (confined to the methods
that need it) so importing this module never pulls the SDK into a process
that lacks it."""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any, Iterator

from oddish.runtime.ports import Capabilities, ExecutionBackend, GpuSupport

logger = logging.getLogger(__name__)


class ModalBackend:
    name = "modal"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            gpu=GpuSupport(
                accelerators=("H100", "H200", "A100", "L40S", "A10G", "T4"),
                max_count=8,
            ),
            private_registry_pull=True,
            network_egress="configurable",
            persistent_volumes=False,
            streaming_logs=True,
            memory_snapshot_fork=True,
            cold_start="minutes",
        )

    def harbor_env_kwargs(self, base_kwargs: dict[str, Any]) -> dict[str, Any]:
        # Modal needs no extra env kwargs today; pass the caller's through.
        return dict(base_kwargs)

    async def teardown(self, external_id: str) -> bool:
        if not external_id:
            return False
        try:
            import modal

            sandbox = await modal.Sandbox.from_id.aio(external_id)
            await sandbox.terminate.aio()
        except Exception:
            logger.exception(
                "ModalBackend.teardown: failed to terminate %s", external_id
            )
            return False
        logger.info("ModalBackend.teardown: terminated %s", external_id)
        return True

    @contextlib.contextmanager
    def capture_diagnostics(self, job_dir: Path) -> Iterator[Path | None]:
        # Implemented in Task 6.
        yield None


_: ExecutionBackend = ModalBackend()  # structural conformance check at import

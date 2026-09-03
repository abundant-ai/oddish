"""Thunder GPU execution backend.

Harbor owns sandbox creation and normal teardown. This adapter exposes Thunder
to Oddish's capability registry and provides the recovery path used when a
worker is cancelled or dies after persisting the remote sandbox id.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from oddish.runtime.ports import Capabilities, ExecutionBackend, GpuSupport

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ThunderSandboxSnapshot:
    external_id: str
    name: str


ThunderInventoryDelegate = Callable[[], Awaitable[tuple[ThunderSandboxSnapshot, ...]]]
_inventory_delegate: ThunderInventoryDelegate | None = None


def register_thunder_inventory_delegate(delegate: ThunderInventoryDelegate) -> None:
    global _inventory_delegate
    _inventory_delegate = delegate


async def snapshot_thunder_sandboxes() -> tuple[ThunderSandboxSnapshot, ...]:
    """Snapshot active Thunder sandboxes through the credential-owning process."""
    if _inventory_delegate is not None:
        return await _inventory_delegate()
    return await ThunderBackend().snapshot_sandboxes_direct()


class ThunderBackend:
    name = "thunder"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            gpu=GpuSupport(
                accelerators=("A6000", "A100", "H100"),
                max_count=8,
            ),
            private_registry_pull=False,
            network_egress="configurable",
            persistent_volumes=False,
            streaming_logs=True,
            memory_snapshot_fork=False,
            cold_start="seconds",
        )

    def harbor_env_kwargs(self, base_kwargs: dict[str, Any]) -> dict[str, Any]:
        # Thunder's gpu_type, timeout, and retry controls belong to Harbor. Do
        # not replace caller-owned values here.
        return dict(base_kwargs)

    async def teardown(self, external_id: str) -> bool:
        """Best-effort termination for cancellation and orphan recovery.

        A missing sandbox is already in the desired state. All other provider
        failures are logged and converted to False so cleanup never masks the
        trial failure that triggered it.
        """
        if not external_id:
            return False

        try:
            from thunder_sandbox import NotFoundError, Sandbox

            try:
                sandbox = await Sandbox.from_id_async(external_id)
            except NotFoundError:
                logger.info(
                    "metric=thunder.sandbox_gone phase=teardown external_id=%s",
                    external_id,
                )
                return True
            await sandbox.terminate_async()
        except Exception:
            logger.exception(
                "ThunderBackend.teardown: failed to terminate %s", external_id
            )
            return False

        logger.info("ThunderBackend.teardown: terminated %s", external_id)
        return True

    async def snapshot_sandboxes_direct(self) -> tuple[ThunderSandboxSnapshot, ...]:
        """List active sandboxes without making ownership decisions."""
        from thunder_sandbox import Client

        snapshots: list[ThunderSandboxSnapshot] = []
        async with Client.from_cli() as client:
            async for sandbox in client.list_sandboxes_async(status="active"):
                snapshots.append(
                    ThunderSandboxSnapshot(
                        external_id=str(sandbox.id),
                        name=str(sandbox.name),
                    )
                )
        return tuple(snapshots)

    @contextlib.contextmanager
    def capture_diagnostics(self, job_dir: Path) -> Iterator[Path | None]:
        del job_dir
        yield None


_: ExecutionBackend = ThunderBackend()  # structural conformance check

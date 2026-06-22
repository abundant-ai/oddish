"""Hermetic in-memory backend: the conformance target and the Phase B+
test double. No network, no SDK imports."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any, Iterator

from oddish.runtime.ports import Capabilities, ExecutionBackend


class FakeBackend:
    name = "fake"

    def __init__(self) -> None:
        self.torn_down: list[str] = []

    def capabilities(self) -> Capabilities:
        return Capabilities(
            gpu=None,
            private_registry_pull=True,
            network_egress="allow",
            persistent_volumes=True,
            streaming_logs=True,
            memory_snapshot_fork=True,
            cold_start="instant",
        )

    def harbor_env_kwargs(self, base_kwargs: dict[str, Any]) -> dict[str, Any]:
        return dict(base_kwargs)

    async def teardown(self, external_id: str) -> bool:
        if not external_id:
            return False
        self.torn_down.append(external_id)
        return True

    @contextlib.contextmanager
    def capture_diagnostics(self, job_dir: Path) -> Iterator[Path | None]:
        yield None


_: ExecutionBackend = FakeBackend()  # structural conformance check at import

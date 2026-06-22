"""Hermetic in-memory dispatcher: the conformance target and test double.

No infrastructure, no I/O. Records what it spawned/cancelled so tests can make
assertions about the control-plane contract.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Mapping, Sequence

from oddish.dispatch.ports import Dispatcher, WorkerHandle


class FakeDispatcher:
    name = "fake"

    def __init__(self) -> None:
        self.spawned: list[WorkerHandle] = []
        self.cancelled: set[str] = set()
        self._counter = 0

    async def spawn(self, *, spawn_plan: Sequence[str]) -> Sequence[WorkerHandle]:
        handles: list[WorkerHandle] = []
        for queue_key in spawn_plan:
            self._counter += 1
            handle = WorkerHandle(
                provider=self.name,
                queue_key=queue_key,
                id=f"fake-{self._counter}",
                provisional=False,
            )
            handles.append(handle)
            self.spawned.append(handle)
        return handles

    async def check_active(
        self, handles: Sequence[WorkerHandle]
    ) -> AsyncIterator[WorkerHandle]:
        for handle in handles:
            if handle.id not in self.cancelled:
                yield handle

    async def cancel(self, handles: Sequence[WorkerHandle]) -> int:
        cancelled = 0
        for handle in handles:
            if handle.id and handle.id not in self.cancelled:
                self.cancelled.add(handle.id)
                cancelled += 1
        return cancelled

    async def recover(self, serialized: Mapping[str, Any]) -> WorkerHandle | None:
        return WorkerHandle.deserialize(serialized)


_: Dispatcher = FakeDispatcher()  # structural conformance check at import

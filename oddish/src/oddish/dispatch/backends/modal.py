"""Modal dispatcher: the reference adapter (design spec §6.2).

Wraps today's two Modal-locked control-plane primitives unchanged:

* **spawn** — ``process_single_job.spawn.aio(queue_key=...)`` fanned out with
  ``asyncio.gather`` (``backend/worker/functions.py``). The ``spawn.aio()``
  return is discarded; the worker self-reports its ``current_function_call_id``
  into its claimed ``worker_jobs`` row. So ``spawn`` returns **provisional**
  handles with an empty id — the spawner never owns it (spec §5.2/§11).
* **cancel** — ``modal.FunctionCall.from_id(id).cancel.aio(
  terminate_containers=True)`` batched (``backend/api/routers/tasks.py``).

The Modal ``Function`` to spawn is **injected** so this oddish-package module
never imports backend code; ``import modal`` stays lazy (confined to ``cancel``)
so importing this module never pulls the SDK into a process that lacks it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Sequence

from oddish.dispatch.ports import Dispatcher, WorkerHandle

logger = logging.getLogger(__name__)

# Matches the legacy backend batch size so cancel behavior is byte-identical.
MODAL_CANCEL_BATCH_SIZE = 32

CancelFn = Callable[[list[str]], Awaitable[int]]


async def _default_cancel_function_calls(fc_ids: list[str]) -> int:
    """Terminate Modal worker containers by function-call id.

    Lifted verbatim from ``backend/api/routers/tasks.py:_cancel_modal_function_calls``
    so the cancel path's behavior is unchanged when it flows through the
    dispatcher instead of the hard-wired reach-in.
    """
    if not fc_ids:
        return 0

    try:
        import modal
    except ImportError:
        return 0

    unique_fc_ids = list(dict.fromkeys(fc_ids))
    cancelled = 0

    async def cancel_one(fc_id: str) -> bool:
        try:
            fc = modal.FunctionCall.from_id(fc_id)
            await fc.cancel.aio(terminate_containers=True)
            return True
        except Exception:
            return False

    for start in range(0, len(unique_fc_ids), MODAL_CANCEL_BATCH_SIZE):
        batch = unique_fc_ids[start : start + MODAL_CANCEL_BATCH_SIZE]
        results = await asyncio.gather(*(cancel_one(fc_id) for fc_id in batch))
        cancelled += sum(1 for result in results if result)

    return cancelled


class ModalDispatcher:
    name = "modal"

    def __init__(
        self,
        *,
        spawn_function: Any | None = None,
        cancel_fn: CancelFn | None = None,
    ) -> None:
        # ``spawn_function`` is the Modal ``process_single_job`` Function,
        # injected by the backend wiring (oddish must not import backend code).
        self._spawn_function = spawn_function
        self._cancel_fn: CancelFn = cancel_fn or _default_cancel_function_calls
        self._cancelled: set[str] = set()

    async def spawn(self, *, spawn_plan: Sequence[str]) -> Sequence[WorkerHandle]:
        if not spawn_plan:
            return []
        if self._spawn_function is None:
            raise RuntimeError(
                "ModalDispatcher.spawn requires a Modal Function; inject "
                "spawn_function=process_single_job at wiring time."
            )
        await asyncio.gather(
            *(
                self._spawn_function.spawn.aio(queue_key=queue_key)
                for queue_key in spawn_plan
            )
        )
        # Provisional handles: the durable id is worker-written, not returned.
        return [
            WorkerHandle(provider=self.name, queue_key=queue_key, id="")
            for queue_key in spawn_plan
        ]

    async def check_active(
        self, handles: Sequence[WorkerHandle]
    ) -> AsyncIterator[WorkerHandle]:
        # worker_jobs heartbeats are the source of truth for liveness (spec
        # §5.4); check_active is a best-effort over handles with durable ids.
        for handle in handles:
            if handle.id and handle.id not in self._cancelled:
                yield handle

    async def cancel(self, handles: Sequence[WorkerHandle]) -> int:
        fc_ids = [
            h.id for h in handles if h.id and h.id not in self._cancelled
        ]
        if not fc_ids:
            return 0
        count = await self._cancel_fn(list(dict.fromkeys(fc_ids)))
        self._cancelled.update(fc_ids)
        return count

    async def recover(self, serialized: Mapping[str, Any]) -> WorkerHandle | None:
        # Modal function-call ids are durable; reattach via FunctionCall.from_id.
        handle = WorkerHandle.deserialize(serialized)
        if not handle.id:
            return None
        return handle


_: Dispatcher = ModalDispatcher()  # structural conformance check at import

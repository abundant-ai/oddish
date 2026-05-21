from oddish.workers.queue.db_proxy import get_db_proxy
from oddish.workers.queue.dispatch_signal import notify_dispatch


async def ensure_queue_slots(queue_key: str, limit: int) -> None:
    if limit <= 0:
        return
    await get_db_proxy().ensure_slots(queue_key=queue_key, limit=limit)


async def acquire_queue_slot(
    *,
    queue_key: str,
    limit: int,
    worker_id: str,
    lease_seconds: int,
) -> int | None:
    if limit <= 0:
        return None
    await ensure_queue_slots(queue_key, limit)
    return await get_db_proxy().acquire_slot(
        queue_key=queue_key,
        limit=limit,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
    )


async def release_queue_slot(
    *,
    queue_key: str,
    slot: int,
    worker_id: str,
) -> None:
    await get_db_proxy().release_slot(
        queue_key=queue_key, slot=slot, worker_id=worker_id
    )
    await notify_dispatch(queue_key)


async def cleanup_stale_queue_slots() -> int:
    return await get_db_proxy().cleanup_stale_slots()

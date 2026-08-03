"""Database-backed weighted capacity leases shared across queue keys."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import logging
import time

import asyncpg

from oddish.config import settings

logger = logging.getLogger(__name__)

WAITING = "WAITING"
HELD = "HELD"


class ProviderCapacityRequestTooLarge(ValueError):
    """Raised when one request cannot fit in the managed provider pool."""


def _validate_capacity_request(
    *, requested_memory_mb: int, memory_limit_mb: int
) -> None:
    if requested_memory_mb <= 0:
        raise ValueError("requested_memory_mb must be positive")
    if memory_limit_mb <= 0:
        raise ValueError("memory_limit_mb must be positive")
    if requested_memory_mb > memory_limit_mb:
        raise ProviderCapacityRequestTooLarge(
            f"requested memory {requested_memory_mb} MiB exceeds provider "
            f"capacity limit {memory_limit_mb} MiB"
        )


@asynccontextmanager
async def _capacity_connection() -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(
        settings.asyncpg_url,
        statement_cache_size=0,
        server_settings=settings.asyncpg_server_settings(),
    )
    try:
        yield conn
    finally:
        await conn.close()


@dataclass(frozen=True, slots=True)
class ProviderCapacitySnapshot:
    used_memory_mb: int
    memory_limit_mb: int
    held_leases: int
    lease_limit: int
    waiting_leases: int
    waiting_memory_mb: int


async def _try_acquire(
    *,
    provider: str,
    owner_id: str,
    requested_memory_mb: int,
    memory_limit_mb: int,
    lease_limit: int,
    lease_seconds: int,
) -> tuple[bool, ProviderCapacitySnapshot]:
    _validate_capacity_request(
        requested_memory_mb=requested_memory_mb,
        memory_limit_mb=memory_limit_mb,
    )
    async with _capacity_connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"oddish:provider-capacity:{provider}",
            )
            await conn.execute(
                "DELETE FROM provider_capacity_leases "
                "WHERE provider = $1 AND lease_expires_at <= NOW()",
                provider,
            )
            await conn.execute(
                """
                INSERT INTO provider_capacity_leases (
                    provider, owner_id, requested_memory_mb, state,
                    heartbeat_at, lease_expires_at
                ) VALUES ($1, $2, $3, 'WAITING', NOW(),
                          NOW() + make_interval(secs => $4))
                ON CONFLICT (provider, owner_id) DO UPDATE
                SET heartbeat_at = NOW(),
                    lease_expires_at = NOW() + make_interval(secs => $4),
                    requested_memory_mb = CASE
                        WHEN provider_capacity_leases.state = 'WAITING' THEN $3
                        ELSE provider_capacity_leases.requested_memory_mb
                    END
                """,
                provider,
                owner_id,
                requested_memory_mb,
                lease_seconds,
            )
            state = await conn.fetchval(
                "SELECT state FROM provider_capacity_leases "
                "WHERE provider = $1 AND owner_id = $2",
                provider,
                owner_id,
            )
            totals = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(requested_memory_mb)
                           FILTER (WHERE state = 'HELD'), 0) AS used_memory_mb,
                       COUNT(*) FILTER (WHERE state = 'HELD') AS held_leases,
                       COUNT(*) FILTER (WHERE state = 'WAITING') AS waiting_leases,
                       COALESCE(SUM(requested_memory_mb)
                           FILTER (WHERE state = 'WAITING'), 0) AS waiting_memory_mb
                FROM provider_capacity_leases
                WHERE provider = $1
                """,
                provider,
            )
            used_memory_mb = int(totals["used_memory_mb"] or 0)
            held_leases = int(totals["held_leases"] or 0)
            if state != HELD:
                first_waiter = await conn.fetchval(
                    """
                    SELECT owner_id
                    FROM provider_capacity_leases
                    WHERE provider = $1 AND state = 'WAITING'
                    ORDER BY created_at, owner_id
                    LIMIT 1
                    """,
                    provider,
                )
                if (
                    first_waiter == owner_id
                    and held_leases < lease_limit
                    and used_memory_mb + requested_memory_mb <= memory_limit_mb
                ):
                    await conn.execute(
                        """
                        UPDATE provider_capacity_leases
                        SET state = 'HELD', acquired_at = NOW(), heartbeat_at = NOW(),
                            lease_expires_at = NOW() + make_interval(secs => $3)
                        WHERE provider = $1 AND owner_id = $2 AND state = 'WAITING'
                        """,
                        provider,
                        owner_id,
                        lease_seconds,
                    )
                    state = HELD
                    used_memory_mb += requested_memory_mb
                    held_leases += 1
                    totals = dict(totals)
                    totals["waiting_leases"] = int(totals["waiting_leases"] or 0) - 1
                    totals["waiting_memory_mb"] = (
                        int(totals["waiting_memory_mb"] or 0) - requested_memory_mb
                    )

    return state == HELD, ProviderCapacitySnapshot(
        used_memory_mb=used_memory_mb,
        memory_limit_mb=memory_limit_mb,
        held_leases=held_leases,
        lease_limit=lease_limit,
        waiting_leases=int(totals["waiting_leases"] or 0),
        waiting_memory_mb=int(totals["waiting_memory_mb"] or 0),
    )


async def _heartbeat(*, provider: str, owner_id: str, lease_seconds: int) -> bool:
    async with _capacity_connection() as conn:
        result = await conn.execute(
            """
            UPDATE provider_capacity_leases
            SET heartbeat_at = NOW(),
                lease_expires_at = NOW() + make_interval(secs => $3)
            WHERE provider = $1 AND owner_id = $2 AND state = 'HELD'
            """,
            provider,
            owner_id,
            lease_seconds,
        )
    return str(result).split()[-1] == "1"


async def _release(*, provider: str, owner_id: str) -> None:
    async with _capacity_connection() as conn:
        await conn.execute(
            "DELETE FROM provider_capacity_leases "
            "WHERE provider = $1 AND owner_id = $2",
            provider,
            owner_id,
        )


@dataclass(slots=True)
class ProviderCapacityLease:
    provider: str
    owner_id: str
    requested_memory_mb: int
    lease_seconds: int
    heartbeat_seconds: int
    _stop: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _heartbeat_task: asyncio.Task[None] | None = field(default=None, init=False)
    _owner_task: asyncio.Task[object] | None = field(default=None, init=False)
    _release_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _released: bool = field(default=False, init=False)

    def start(self) -> None:
        self._owner_task = asyncio.current_task()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        last_success = time.monotonic()
        while True:
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.heartbeat_seconds
                )
                return
            except TimeoutError:
                pass
            try:
                if not await _heartbeat(
                    provider=self.provider,
                    owner_id=self.owner_id,
                    lease_seconds=self.lease_seconds,
                ):
                    logger.error(
                        "provider capacity lease disappeared provider=%s owner=%s",
                        self.provider,
                        self.owner_id,
                    )
                    if self._owner_task is not None:
                        self._owner_task.cancel()
                    return
                last_success = time.monotonic()
            except Exception:
                logger.exception(
                    "provider capacity heartbeat failed provider=%s owner=%s",
                    self.provider,
                    self.owner_id,
                )
                if (
                    time.monotonic() - last_success
                    >= self.lease_seconds - self.heartbeat_seconds
                ):
                    if self._owner_task is not None:
                        self._owner_task.cancel()
                    return

    async def release(self) -> bool:
        async with self._release_lock:
            if self._released:
                return True
            self._stop.set()
            if self._heartbeat_task is not None:
                await asyncio.gather(self._heartbeat_task, return_exceptions=True)
            try:
                await _release(provider=self.provider, owner_id=self.owner_id)
            except Exception:
                logger.exception(
                    "provider capacity release failed provider=%s owner=%s",
                    self.provider,
                    self.owner_id,
                )
                return False
            self._released = True
            return True


async def wait_for_provider_capacity(
    *,
    provider: str,
    owner_id: str,
    requested_memory_mb: int,
    memory_limit_mb: int,
    lease_limit: int,
    lease_seconds: int,
    heartbeat_seconds: int,
    poll_seconds: float,
    wait_heartbeat: Callable[[], Awaitable[None]] | None = None,
    wait_check: Callable[[], Awaitable[None]] | None = None,
) -> ProviderCapacityLease:
    """Wait FIFO for both weighted memory and hard lease-count capacity."""
    _validate_capacity_request(
        requested_memory_mb=requested_memory_mb,
        memory_limit_mb=memory_limit_mb,
    )
    started = time.monotonic()
    next_wait_heartbeat = started
    next_log = started
    try:
        while True:
            now = time.monotonic()
            if wait_check is not None:
                await wait_check()
            if wait_heartbeat is not None and now >= next_wait_heartbeat:
                await wait_heartbeat()
                next_wait_heartbeat = now + heartbeat_seconds
            acquired, snapshot = await _try_acquire(
                provider=provider,
                owner_id=owner_id,
                requested_memory_mb=requested_memory_mb,
                memory_limit_mb=memory_limit_mb,
                lease_limit=lease_limit,
                lease_seconds=lease_seconds,
            )
            if acquired:
                lease = ProviderCapacityLease(
                    provider=provider,
                    owner_id=owner_id,
                    requested_memory_mb=requested_memory_mb,
                    lease_seconds=lease_seconds,
                    heartbeat_seconds=heartbeat_seconds,
                )
                lease.start()
                return lease

            now = time.monotonic()
            if now >= next_log:
                logger.info(
                    "provider capacity waiting provider=%s owner=%s requested_mb=%d "
                    "used_mb=%d limit_mb=%d held=%d lease_limit=%d waiting=%d",
                    provider,
                    owner_id,
                    requested_memory_mb,
                    snapshot.used_memory_mb,
                    snapshot.memory_limit_mb,
                    snapshot.held_leases,
                    snapshot.lease_limit,
                    snapshot.waiting_leases,
                )
                next_log = now + 60
            await asyncio.sleep(poll_seconds)
    except BaseException:
        try:
            await asyncio.shield(_release(provider=provider, owner_id=owner_id))
        except Exception:
            logger.exception(
                "provider capacity waiter cleanup failed provider=%s owner=%s",
                provider,
                owner_id,
            )
        raise


async def cleanup_stale_provider_capacity_leases() -> int:
    async with _capacity_connection() as conn:
        result = await conn.execute(
            "DELETE FROM provider_capacity_leases WHERE lease_expires_at <= NOW()"
        )
    try:
        return int(str(result).split()[-1])
    except (TypeError, ValueError):
        return 0

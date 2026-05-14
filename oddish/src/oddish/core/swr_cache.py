"""Tiny in-memory stale-while-revalidate cache.

Designed for read-heavy endpoints whose payloads can tolerate a few
seconds of staleness but whose recompute is expensive enough that
blocking on it kills TTFB.  Per-process; if you need a shared cache
across replicas, swap this out for Redis later.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Generic, TypeVar

T = TypeVar("T")


class SWRCache(Generic[T]):
    """Stale-while-revalidate cache keyed by string.

    Behavior on a ``get_or_compute(key, compute)`` call:

    * age <= ``fresh_seconds``        -> return cached, no work
    * ``fresh`` < age <= ``stale_max`` -> return cached, fire-and-forget a
                                          background recompute (deduped
                                          per key)
    * no entry or age > ``stale_max`` -> block on ``compute()`` and cache

    The ``compute`` callable is awaited; for background refreshes it must
    be self-sufficient (own session, own error handling at the boundary).
    Exceptions during background refresh are swallowed -- the existing
    cache entry stays in place so subsequent requests still get a fast
    served-from-cache response.
    """

    def __init__(
        self,
        *,
        fresh_seconds: float = 10.0,
        stale_max_seconds: float = 300.0,
        max_size: int = 100,
    ) -> None:
        self._cache: dict[str, tuple[T, float]] = {}
        self._in_flight: set[str] = set()
        self._fresh = fresh_seconds
        self._stale_max = stale_max_seconds
        self._max_size = max_size

    def peek(self, key: str) -> tuple[T, float] | None:
        """Return (payload, age_seconds) without mutating the cache."""
        entry = self._cache.get(key)
        if entry is None:
            return None
        value, stored_at = entry
        return value, time.time() - stored_at

    def set(self, key: str, value: T) -> None:
        if len(self._cache) >= self._max_size:
            # Drop the oldest quarter when full -- cheap LRU-ish eviction.
            sorted_keys = sorted(
                self._cache.keys(), key=lambda k: self._cache[k][1]
            )
            for k in sorted_keys[: self._max_size // 4]:
                del self._cache[k]
        self._cache[key] = (value, time.time())

    async def get_or_compute(
        self,
        key: str,
        compute: Callable[[], Awaitable[T]],
        *,
        background_compute: Callable[[], Awaitable[T]] | None = None,
    ) -> tuple[T, bool]:
        """Return ``(value, was_cache_hit)``.

        ``background_compute`` is used for stale-revalidate refreshes when
        the cached entry is still served; it should own its own session /
        resources since the foreground request has already returned by
        the time it runs.  Falls back to ``compute`` if not provided.
        """
        peek = self.peek(key)
        if peek is not None:
            cached, age = peek
            if age <= self._stale_max:
                if age > self._fresh and key not in self._in_flight:
                    self._in_flight.add(key)
                    bg = background_compute or compute
                    asyncio.create_task(self._refresh(key, bg))
                return cached, True

        value = await compute()
        self.set(key, value)
        return value, False

    async def _refresh(
        self, key: str, compute: Callable[[], Awaitable[T]]
    ) -> None:
        try:
            value = await compute()
            self.set(key, value)
        except Exception:
            # Best-effort revalidation; leave the existing entry alone so
            # the next request still hits cache fast.
            pass
        finally:
            self._in_flight.discard(key)

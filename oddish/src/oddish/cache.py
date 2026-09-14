"""One small in-process TTL cache for the many "remember this for a minute"
spots in the request path.

Every entry carries its own expiry, so one cache can hold values with
different lifetimes (the auth cache keeps API-key principals for a minute
and Clerk identities for fifteen). Bounded by ``max_size`` with
least-recently-used eviction so a container can never grow one of these
without limit. Not shared across containers: a value cached here is only
as fresh as this process, and callers that write the underlying data call
``invalidate`` locally and rely on the TTL for other containers.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Generic, TypeVar

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class TTLCache(Generic[K, V]):
    def __init__(
        self,
        ttl_seconds: float,
        *,
        max_size: int = 1000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self._max_size = max_size
        self._clock = clock
        self._data: OrderedDict[K, tuple[V, float]] = OrderedDict()

    def get(self, key: K) -> V | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at <= self._clock():
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: K, value: V, *, ttl_seconds: float | None = None) -> None:
        """Store ``value``; a non-positive TTL (default or override) stores nothing."""
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        if ttl <= 0:
            self._data.pop(key, None)
            return
        now = self._clock()
        self._data[key] = (value, now + ttl)
        self._data.move_to_end(key)
        self._purge(now)

    def invalidate(self, key: K) -> bool:
        return self._data.pop(key, None) is not None

    def invalidate_where(self, predicate: Callable[[K], bool]) -> int:
        keys = [key for key in self._data if predicate(key)]
        for key in keys:
            self._data.pop(key, None)
        return len(keys)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)

    def _purge(self, now: float) -> None:
        for key in [k for k, (_, exp) in self._data.items() if exp <= now]:
            self._data.pop(key, None)
        while len(self._data) > self._max_size:
            self._data.popitem(last=False)

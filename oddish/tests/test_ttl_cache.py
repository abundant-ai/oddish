"""``oddish.cache.TTLCache``: per-entry expiry, LRU bound, invalidation."""

from __future__ import annotations

from oddish.cache import TTLCache


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_entries_expire_on_their_own_ttl():
    clock = _Clock()
    cache: TTLCache[str, str] = TTLCache(60, clock=clock)
    cache.set("short", "a")
    cache.set("long", "b", ttl_seconds=900)

    clock.now += 61
    assert cache.get("short") is None
    assert cache.get("long") == "b"

    clock.now += 900
    assert cache.get("long") is None


def test_non_positive_ttl_stores_nothing():
    cache: TTLCache[str, str] = TTLCache(0)
    cache.set("k", "v")
    assert cache.get("k") is None
    assert len(cache) == 0

    cache = TTLCache(60)
    cache.set("k", "v")
    cache.set("k", "v2", ttl_seconds=0)
    assert cache.get("k") is None


def test_size_bound_evicts_least_recently_used():
    cache: TTLCache[str, int] = TTLCache(60, max_size=2)
    cache.set("a", 1)
    cache.set("b", 2)
    assert cache.get("a") == 1  # touch: "b" is now the oldest
    cache.set("c", 3)

    assert cache.get("b") is None
    assert cache.get("a") == 1
    assert cache.get("c") == 3


def test_invalidate_by_key_and_predicate():
    cache: TTLCache[str, int] = TTLCache(60)
    cache.set("clerk:u1:org", 1)
    cache.set("clerk:u1:no-org", 2)
    cache.set("clerk:u2:org", 3)
    cache.set("apikey:x", 4)

    assert cache.invalidate("apikey:x") is True
    assert cache.invalidate("apikey:x") is False
    assert cache.invalidate_where(lambda key: key.startswith("clerk:u1:")) == 2
    assert cache.get("clerk:u1:org") is None
    assert cache.get("clerk:u2:org") == 3

    cache.clear()
    assert len(cache) == 0

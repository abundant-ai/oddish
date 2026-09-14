"""``ModalDictSharedCache`` is installed at API startup and receives every call
``oddish.core.dashboard`` makes on the shared backend, so it must override each
method the base class leaves raising ``NotImplementedError``. A missing override
only surfaces in production (``invalidate_dashboard_cache`` after a tag edit),
never in tests that run on the in-process backend."""

from __future__ import annotations

from dashboard_cache import _QUEUE_PIPELINE_PREFIX, ModalDictSharedCache
from oddish.core.dashboard import DashboardSharedCacheBackend


def _base_methods() -> set[str]:
    return {
        name
        for name, value in vars(DashboardSharedCacheBackend).items()
        if callable(value) and not name.startswith("__")
    }


def test_modal_cache_overrides_every_base_method() -> None:
    base = _base_methods()
    assert base, "base backend defines no methods; the contract moved"
    missing = sorted(name for name in base if name not in vars(ModalDictSharedCache))
    assert not missing, f"ModalDictSharedCache does not override {missing}"


def _cache_with(entries: dict[str, tuple[object, float]]) -> ModalDictSharedCache:
    # Bypass __init__ so no Modal Dict is created; the methods only need a
    # mapping with get / pop / keys, which a plain dict provides.
    cache = ModalDictSharedCache.__new__(ModalDictSharedCache)
    cache._dict = entries  # type: ignore[assignment]
    return cache


def test_invalidate_org_pops_one_org_and_none_flushes_the_prefix() -> None:
    cache = _cache_with(
        {
            f"{_QUEUE_PIPELINE_PREFIX}org-a:": ("a", 0.0),
            f"{_QUEUE_PIPELINE_PREFIX}org-b:": ("b", 0.0),
            "unrelated": ("c", 0.0),
        }
    )
    cache.invalidate_org("org-a")
    assert set(cache._dict) == {f"{_QUEUE_PIPELINE_PREFIX}org-b:", "unrelated"}
    cache.invalidate_org(None)
    assert set(cache._dict) == {"unrelated"}

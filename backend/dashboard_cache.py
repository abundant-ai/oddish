"""Modal Dict-backed shared cache for the dashboard's queue/pipeline slice.

The dashboard's slowest query is a full aggregate over the whole ``trials``
table (queue + pipeline stats). It depends only on ``org_id``, so it caches as
one entry per org. By default ``oddish.core.dashboard`` caches it in
process-local memory, which a cold Modal container never sees -- so every cold
container re-runs the multi-second scan.

This module installs a cross-container backend backed by a ``modal.Dict`` so
the scan runs at most once per org per TTL across the entire API fleet, and a
freshly-started container reads the warm entry instead of recomputing it.

The Dict is referenced by name with ``create_if_missing=True`` -- no manual
Modal console setup is needed, and named objects are scoped to the Modal
environment, so prod and PR previews never share an entry. See
``oddish.core.dashboard.DashboardSharedCacheBackend`` for the contract.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import modal

from oddish.core.dashboard import (
    DashboardSharedCacheBackend,
    set_dashboard_shared_cache_backend,
)

logger = logging.getLogger(__name__)

_DICT_NAME = "oddish-dashboard-cache"
_QUEUE_PIPELINE_PREFIX = "dashboard.queue_pipeline:"


class ModalDictSharedCache(DashboardSharedCacheBackend):
    """Shared dashboard cache stored in a ``modal.Dict``.

    Every method is best-effort: a Modal Dict hiccup degrades to a cache miss
    (recompute on this request) rather than failing the dashboard. ``modal.Dict``
    has no native TTL, so entries are ``(data, stored_at_epoch)`` tuples and the
    TTL is enforced on read.
    """

    def __init__(self, dict_name: str = _DICT_NAME) -> None:
        # Lazily created on first use; scoped to the Modal environment.
        self._dict = modal.Dict.from_name(dict_name, create_if_missing=True)

    def get(self, cache_key: str, ttl_seconds: int) -> Any | None:
        try:
            entry = self._dict.get(cache_key)
        except Exception:
            logger.warning("dashboard shared cache get failed", exc_info=True)
            return None
        if not entry:
            return None
        data, stored_at = entry
        if time.time() - stored_at > ttl_seconds:
            return None
        return data

    def set(self, cache_key: str, data: Any) -> None:
        try:
            self._dict[cache_key] = (data, time.time())
        except Exception:
            logger.warning("dashboard shared cache set failed", exc_info=True)

    def invalidate_org(self, org_id: str | None) -> None:
        try:
            if org_id is None:
                # Global flush (admin / tests): drop every queue/pipeline entry.
                for key in list(self._dict.keys()):
                    if key.startswith(_QUEUE_PIPELINE_PREFIX):
                        self._dict.pop(key, None)
                return
            # One entry per org -- an exact-key pop, no scan.
            self._dict.pop(f"{_QUEUE_PIPELINE_PREFIX}{org_id}:", None)
        except Exception:
            logger.warning("dashboard shared cache invalidate failed", exc_info=True)


async def precompute_dashboard_queue_pipeline() -> int:
    """Refresh every org's cached queue/pipeline slice in one grouped scan.

    Computes per-org queue + pipeline stats with a single grouped pass over
    ``trials`` / ``tasks`` (instead of one scan per org) and writes each org's
    payload into the shared Modal Dict the dashboard reads. Returns the number
    of orgs refreshed.

    This is the engine behind fix #2: the expensive whole-table aggregate runs
    here, on a schedule, once for the whole fleet -- so the dashboard request
    path reads a warm entry and never scans. Safe to call repeatedly.
    """
    # Ensure writes land in the shared Dict (the worker process installs the
    # backend lazily here rather than depending on a separate startup hook).
    install_modal_dashboard_cache()

    from oddish.core.dashboard import store_queue_pipeline_slice
    from oddish.db import get_session
    from oddish.queue import get_queue_and_pipeline_stats_by_org

    async with get_session() as session:
        stats_by_org = await get_queue_and_pipeline_stats_by_org(session)

    for org_id, payload in stats_by_org.items():
        store_queue_pipeline_slice(org_id, payload)
    return len(stats_by_org)


def install_modal_dashboard_cache() -> bool:
    """Route the dashboard queue/pipeline slice through a shared Modal Dict.

    Best-effort: on any failure (e.g. running outside a Modal runtime in local
    dev) we leave the process-local default in place so the dashboard still
    works, just without cross-container sharing. Returns True when the shared
    backend was installed.
    """
    try:
        set_dashboard_shared_cache_backend(ModalDictSharedCache())
    except Exception:
        logger.warning("could not install Modal dashboard cache", exc_info=True)
        return False
    logger.info("installed Modal Dict dashboard cache (%s)", _DICT_NAME)
    return True

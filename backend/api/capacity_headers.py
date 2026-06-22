"""Advertise a recommended submission ceiling on submit/sweep/upload responses.

Inert on the client (extra response headers only; no body change). Reads the
~5s-cached load snapshot; never fails the request if the snapshot is unavailable.
"""

from __future__ import annotations

from starlette.requests import Request

from oddish.core.admin import SUBMIT_CONCURRENCY_HEADER, get_cached_load_snapshot

# Paths whose responses advertise capacity (submit + upload only). Prefix match
# so /tasks/sweep and /tasks/sweep/batch both qualify.
_CAPACITY_PATHS = (
    "/tasks/sweep",
    "/tasks/upload/init",
    "/tasks/upload/complete",
)


def _is_capacity_path(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in _CAPACITY_PATHS)


async def capacity_header_middleware(request: Request, call_next):
    response = await call_next(request)
    if not _is_capacity_path(request.url.path):
        return response
    try:
        snap = await get_cached_load_snapshot()
    except Exception:  # noqa: BLE001 - advertising is best-effort, never break a request
        return response
    ceiling = snap.submit_ceiling
    response.headers[SUBMIT_CONCURRENCY_HEADER] = (
        f"ceiling={ceiling}; pressure={snap.pressure:.2f}; ttl={snap.ttl_seconds}"
    )
    response.headers["RateLimit-Policy"] = (
        f"submit;q={ceiling};qu=concurrent-requests;w=0"
    )
    response.headers["RateLimit"] = f"submit;r={ceiling};t={snap.ttl_seconds}"
    return response

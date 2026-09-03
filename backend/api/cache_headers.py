"""Browser cache policy for reads the dashboard fetches directly.

While the dashboard reached the backend through its Next.js ``/api/*`` proxy
routes, those routes set ``Cache-Control`` on a handful of responses (file
listings that carry 15-minute presigned URLs, the dashboard summary, the
member list). With ``NEXT_PUBLIC_API_DIRECT`` the browser talks to this API
itself, so the same policy has to come from here. Keyed on the matched route
template rather than the URL so a path parameter can never widen a match, and
applied only when the handler set nothing itself (the task-file content route
sets its own ``ETag`` / ``no-cache`` pair).
"""

from __future__ import annotations

from fastapi import Request

_PRIVATE_LISTING = "private, max-age=600, stale-while-revalidate=60"
_PRIVATE_FILE = "private, max-age=300, stale-while-revalidate=60"
_PUBLIC_LISTING = "public, max-age=600, stale-while-revalidate=60"
_PUBLIC_FILE = "public, max-age=300, stale-while-revalidate=60"

ROUTE_CACHE_CONTROL: dict[str, str] = {
    "/dashboard": "private, max-age=5, stale-while-revalidate=30",
    "/users": "private, max-age=30, stale-while-revalidate=120",
    "/people/search": "no-store",
    "/tasks/{task_id}/open": "no-store",
    # Listings hand out presigned URLs that expire in 15 minutes; ten minutes
    # of reuse keeps every URL in a cached listing valid.
    "/tasks/{task_id}/files": _PRIVATE_LISTING,
    "/trials/{trial_id}/files": _PRIVATE_LISTING,
    "/trials/{trial_id}/files/{file_path:path}": _PRIVATE_FILE,
    "/public/experiments/{public_token}/tasks/{task_id}/files": _PUBLIC_LISTING,
    "/public/experiments/{public_token}/tasks/{task_id}/files/{file_path:path}": (
        _PUBLIC_FILE
    ),
    "/public/experiments/{public_token}/trials/{trial_id}/files": _PUBLIC_LISTING,
    "/public/experiments/{public_token}/trials/{trial_id}/files/{file_path:path}": (
        _PUBLIC_FILE
    ),
}


def cache_control_for(request: Request, status_code: int) -> str | None:
    """The policy for this request, or ``None`` when none applies."""
    if request.method != "GET" or not 200 <= status_code < 300:
        return None
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if not isinstance(path, str):
        return None
    return ROUTE_CACHE_CONTROL.get(path)


async def cache_header_middleware(request: Request, call_next):
    response = await call_next(request)
    if "cache-control" not in response.headers:
        policy = cache_control_for(request, response.status_code)
        if policy is not None:
            response.headers["Cache-Control"] = policy
    return response

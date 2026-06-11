"""ASGI wrapper around the S3 FastMCP server: verifies the signed scope token
on each request, sets the per-request experiment contextvar, and delegates to
the FastMCP streamable-http app.
"""
from __future__ import annotations

import time
from urllib.parse import parse_qs

from oddish.config import settings
from oddish.mcp import s3_server
from oddish.mcp.scope_token import verify_experiment_token


def _signing_key() -> str:
    return settings.mcp_s3_signing_key


def _now() -> int:
    return int(time.time())


def resolve_scope_from_query(query_string: str) -> str:
    params = parse_qs(query_string)
    token = (params.get("token") or [""])[0]
    if not token:
        raise ValueError("missing scope token")
    return verify_experiment_token(token, key=_signing_key(), now=_now())


def create_app():
    """Return the streamable-http ASGI app with scope middleware."""
    inner = s3_server.mcp.streamable_http_app()

    async def app(scope, receive, send):
        if scope["type"] == "http":
            try:
                exp = resolve_scope_from_query(
                    scope.get("query_string", b"").decode()
                )
            except ValueError:
                await _reject(send)
                return
            token = s3_server._current_experiment.set(exp)
            try:
                await inner(scope, receive, send)
            finally:
                s3_server._current_experiment.reset(token)
        else:
            await inner(scope, receive, send)

    return app


async def _reject(send):
    await send({"type": "http.response.start", "status": 401,
                "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"invalid scope token",
                "more_body": False})

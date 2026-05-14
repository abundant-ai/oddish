"""API package.

We configure Logfire here — before importing ``api.app`` — so that
``logfire.install_auto_tracing(modules=["api.routers", "oddish.core",
"oddish.queue", "oddish.workers"])`` can install import hooks before
those modules are loaded by ``api.app`` / ``create_app()``. If we
configured later, the auto-trace patcher would skip already-imported
modules and our backend traces would have no meaningful function-call
shape under the FastAPI request span.

We also wrap the ``from api.app import create_app`` in a
``app.container_init`` span so that one-time module-load side
effects (litellm pricing fetch, handler-registry init, etc.) get
parented under a named root instead of showing up as orphan top-
level spans on container cold start.
"""

from __future__ import annotations

import os

from observability import configure_logfire, span as _otel_span

configure_logfire(
    service_name=os.environ.get("LOGFIRE_SERVICE_NAME", "oddish-backend"),
)

with _otel_span("app.container_init"):
    from api.app import create_app  # noqa: E402

__all__ = ["create_app"]

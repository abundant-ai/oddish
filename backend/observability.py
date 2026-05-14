"""Pydantic Logfire observability wiring for the Oddish backend.

Configures Logfire once per process and auto-instruments FastAPI,
SQLAlchemy, asyncpg, httpx, and system metrics. Safe to call from
multiple entry points (Railway, Modal API, Modal workers) — repeated
calls after a successful configure are no-ops.

If ``LOGFIRE_TOKEN`` is not set the helpers degrade to no-ops so local
dev (and self-hosters) keep working without an account.

Distributed tracing with the browser is handled by mounting the
``logfire.experimental.annotations.LogfireLoggingHandler`` browser
proxy at ``/logfire-proxy/{path:path}`` — the front-end posts OTLP
batches there and the proxy attaches the write token server-side so
the token never ships to the client. W3C ``traceparent`` headers
emitted by ``@pydantic/logfire-browser`` are picked up automatically
by ``logfire.instrument_fastapi`` so a browser span and its FastAPI
child span share a trace id.
"""

from __future__ import annotations

import logging
import os
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

_configured = False
_lock = Lock()


def _resolve_environment() -> str:
    """Coarse env label: ``production`` / ``preview`` / ``development``.

    PR-specific details (Modal app, Railway env name, git sha) ride on
    each span as resource attributes via ``_extra_resource_attributes``
    so dashboards can filter ``deployment.environment == "preview"`` *and*
    drill into ``oddish.pr`` for a single PR.
    """
    explicit = os.environ.get("LOGFIRE_ENVIRONMENT")
    if explicit:
        return explicit

    modal_app = os.environ.get("MODAL_APP_NAME", "")
    if modal_app.startswith("oddish-pr-"):
        return "preview"

    railway_env = (os.environ.get("RAILWAY_ENVIRONMENT_NAME") or "").lower()
    if railway_env in {"production", "prod"}:
        return "production"
    if railway_env in {"preview", "staging"} or railway_env.startswith("pr-"):
        return "preview"

    oddish_env = os.environ.get("ODDISH_ENV")
    if oddish_env:
        return oddish_env

    if modal_app and modal_app != "oddish":
        # Anything that isn't the canonical production Modal app is a
        # preview / experiment by default.
        return "preview"
    if modal_app == "oddish":
        return "production"

    return "development"


def _extra_resource_attributes() -> dict[str, str]:
    """Per-deployment metadata to attach to every span.

    Kept separate from the environment label so a PR preview's spans
    stay grouped under ``deployment.environment=preview`` while still
    being filterable down to a single PR via ``oddish.pr``.
    """
    attrs: dict[str, str] = {}

    modal_app = os.environ.get("MODAL_APP_NAME")
    if modal_app:
        attrs["oddish.modal_app"] = modal_app
        if modal_app.startswith("oddish-pr-"):
            attrs["oddish.pr"] = modal_app.removeprefix("oddish-pr-")

    for key, source in (
        ("oddish.modal_environment", "MODAL_ENVIRONMENT"),
        ("oddish.railway_environment", "RAILWAY_ENVIRONMENT_NAME"),
        ("oddish.git_sha", "RAILWAY_GIT_COMMIT_SHA"),
        ("oddish.git_branch", "RAILWAY_GIT_BRANCH"),
    ):
        value = os.environ.get(source)
        if value:
            attrs[key] = value

    # Fallback git sha for non-Railway hosts (e.g. Modal images burn the
    # commit into ``ODDISH_RELEASE`` / ``GIT_COMMIT_SHA``).
    if "oddish.git_sha" not in attrs:
        sha = os.environ.get("ODDISH_RELEASE") or os.environ.get("GIT_COMMIT_SHA")
        if sha:
            attrs["oddish.git_sha"] = sha

    return attrs


def configure_logfire(service_name: str) -> bool:
    """Initialize Logfire if a write token is available.

    Returns True if Logfire is active in this process, False otherwise
    (missing token or import failure). Subsequent calls are no-ops.
    """
    global _configured
    with _lock:
        if _configured:
            return True

        token = os.environ.get("LOGFIRE_TOKEN")
        if not token:
            logger.info("LOGFIRE_TOKEN not set; skipping Logfire setup (%s)", service_name)
            return False

        try:
            import logfire
        except ImportError:
            logger.warning("logfire not installed; skipping observability setup")
            return False

        # Logfire merges OTEL_RESOURCE_ATTRIBUTES into its resource, so
        # we use that as the portable way to ship extra metadata
        # (PR number, git sha, modal env) without depending on
        # private kwargs of ``logfire.configure``.
        extra_attrs = _extra_resource_attributes()
        if extra_attrs:
            existing = os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")
            merged = ",".join(
                filter(None, [existing, *(f"{k}={v}" for k, v in extra_attrs.items())])
            )
            os.environ["OTEL_RESOURCE_ATTRIBUTES"] = merged

        try:
            logfire.configure(
                service_name=service_name,
                service_version=os.environ.get("ODDISH_RELEASE")
                or os.environ.get("RAILWAY_GIT_COMMIT_SHA")
                or os.environ.get("GIT_COMMIT_SHA"),
                environment=_resolve_environment(),
                send_to_logfire="if-token-present",
                console=False,
            )
        except Exception:
            logger.warning("logfire.configure failed", exc_info=True)
            return False

        _safe_instrument(logfire.instrument_httpx)
        _safe_instrument(logfire.instrument_asyncpg)
        _safe_instrument(logfire.instrument_system_metrics)
        # SQLAlchemy is wired per-engine in oddish.db; we instrument the
        # SQLA library globally so all engines pick it up.
        _safe_instrument(logfire.instrument_sqlalchemy)

        _configured = True
        logger.info("Logfire configured (service=%s)", service_name)
        return True


def _safe_instrument(fn) -> None:
    try:
        fn()
    except Exception:
        logger.warning("logfire instrumentation %s failed", fn.__name__, exc_info=True)


def instrument_fastapi(app: "FastAPI") -> None:
    """Attach Logfire's FastAPI middleware if logfire is active."""
    if not _configured:
        return
    try:
        import logfire

        logfire.instrument_fastapi(app, capture_headers=False)
    except Exception:
        logger.warning("logfire.instrument_fastapi failed", exc_info=True)


def mount_browser_proxy(app: "FastAPI") -> None:
    """Expose ``/logfire-proxy/{path:path}`` for the browser SDK.

    The proxy reuses the server-side ``LOGFIRE_TOKEN`` to attach the
    Authorization header so it never has to ship to the client.
    Without a token configured we simply don't mount the route.
    """
    if not _configured:
        return
    try:
        from fastapi import Request
        from logfire.experimental.forwarding import logfire_proxy
    except Exception:
        logger.warning("logfire browser proxy unavailable", exc_info=True)
        return

    @app.post("/logfire-proxy/{path:path}", include_in_schema=False)
    async def _logfire_browser_proxy(request: Request, path: str):  # noqa: ARG001
        return await logfire_proxy(request)

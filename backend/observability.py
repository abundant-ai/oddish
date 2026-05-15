"""Pydantic Logfire observability wiring for the Oddish backend.

Configures Logfire once per process and auto-instruments FastAPI,
SQLAlchemy, asyncpg, httpx, and system metrics. Safe to call from
multiple entry points (Modal API, Modal workers) — repeated calls
after a successful configure are no-ops.

If ``LOGFIRE_TOKEN`` is not set the helpers degrade to no-ops so local
dev keeps working without an account.

Distributed tracing with the browser is handled by mounting the
proxy at ``/logfire-proxy/{path:path}`` plus a dedicated permissive
CORS shim (``LogfireProxyCORSMiddleware``) so any front-end origin
— prod, Vercel preview URLs, localhost — can ship spans without
being enumerated in ``CORS_ALLOWED_ORIGINS``. W3C ``traceparent``
headers emitted by ``@pydantic/logfire-browser`` are picked up
automatically by ``logfire.instrument_fastapi`` so a browser span
and its FastAPI child span share a trace id.
"""

from __future__ import annotations

import logging
import os
from threading import Lock
from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

_configured = False
_lock = Lock()


def _resolve_environment() -> str:
    """Per-PR Logfire environment so previews don't share one bucket."""
    explicit = os.environ.get("LOGFIRE_ENVIRONMENT")
    if explicit:
        return explicit

    modal_app = os.environ.get("MODAL_APP_NAME", "")
    if modal_app == "oddish":
        return "production"
    if modal_app.startswith("oddish-pr-"):
        pr = modal_app.removeprefix("oddish-pr-")
        if pr:
            return f"preview-pr-{pr}"
    return "preview"


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

    modal_env = os.environ.get("MODAL_ENVIRONMENT")
    if modal_env:
        attrs["oddish.modal_environment"] = modal_env

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
            logger.info(
                "LOGFIRE_TOKEN not set; skipping Logfire setup (%s)", service_name
            )
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
        # SQLAlchemy instrumentation walks the expression tree on every
        # execute, which is meaningful overhead on hot paths like the
        # dashboard aggregator. ``instrument_asyncpg`` already gives us
        # query-level visibility one layer down, so the SQLA wrapper is
        # gated behind an explicit opt-in env var. Set
        # ``ODDISH_LOGFIRE_INSTRUMENT_SQLA=1`` in environments where
        # the extra ORM-level detail is worth the cost (typically a
        # debug session, not steady-state production).
        if os.environ.get("ODDISH_LOGFIRE_INSTRUMENT_SQLA", "").lower() in (
            "1",
            "true",
            "yes",
        ):
            _safe_instrument(logfire.instrument_sqlalchemy)

        # Give traces a meaningful shape: every function call inside our
        # own packages becomes a span (above the duration floor) so
        # the auto-instrumented HTTP / DB / httpx spans nest under
        # business-named parents like ``api.routers.trials.cancel`` or
        # ``worker.functions.process_single_job`` instead of floating
        # at trace root with no context.
        #
        # We deliberately exclude ``oddish.core`` and ``oddish.queue``:
        # those packages contain helpers (``_resolve_trial_cost``,
        # ``_build_task_status_response``, ``_normalize_worker_job_kind``,
        # ``fetch_visible_worker_jobs``, etc.) called many times per
        # request. Auto-tracing wraps every call with span machinery
        # regardless of ``min_duration``, so even when the span is
        # discarded the wrapping overhead applies. Keep the entry-point
        # surface (`api.routers`, `worker.functions`) instrumented for
        # trace shape; rely on the auto-instrumented asyncpg/httpx
        # spans for the inner work.
        #
        # ``check_imported_modules='ignore'`` is intentional: the
        # ``api`` / ``worker`` PACKAGE objects are inevitably already
        # in ``sys.modules`` by the time we're called (because this
        # call lives inside ``api/__init__.py`` / ``worker/__init__.py``).
        # The submodules we actually want to trace (``api.routers.*``,
        # ``worker.functions``) are NOT yet imported, so the import-hook
        # still fires for them — but Logfire would otherwise spam a
        # warning about the already-imported package roots.
        try:
            logfire.install_auto_tracing(
                modules=[
                    "api.routers",
                    "worker.functions",
                ],
                min_duration=0.25,
                check_imported_modules="ignore",
            )
        except Exception:
            logger.warning("logfire.install_auto_tracing failed", exc_info=True)

        _configured = True
        logger.info("Logfire configured (service=%s)", service_name)
        return True


def _safe_instrument(fn) -> None:
    try:
        fn()
    except Exception:
        logger.warning("logfire instrumentation %s failed", fn.__name__, exc_info=True)


def span(name: str, /, **attributes):
    """Open a Logfire span, or a no-op context manager when disabled.

    Use this at top-level entry points (cron-like worker cycles,
    background tasks, anything that isn't already wrapped by FastAPI
    or a job-runner span) so child auto-instrumented spans nest under
    a meaningful named parent instead of floating at the trace root.
    """
    if _configured:
        try:
            import logfire

            return logfire.span(name, **attributes)
        except Exception:
            logger.warning("logfire.span(%r) failed", name, exc_info=True)
    from contextlib import nullcontext

    return nullcontext()


def instrument_fastapi(app: "FastAPI") -> None:
    """Attach Logfire's FastAPI middleware if logfire is active.

    We deliberately do NOT pass ``excluded_urls`` — every HTTP entry
    point (including ``/logfire-proxy/*``, ``/openapi.json``, health
    pings) should appear as its own top-level span so the trace
    captures *what actually came in*. If something is too chatty,
    deal with it via sampling on the Logfire side, not by dropping
    spans at the source.
    """
    if not _configured:
        return
    try:
        import logfire

        logfire.instrument_fastapi(app, capture_headers=False)
    except Exception:
        logger.warning("logfire.instrument_fastapi failed", exc_info=True)


class LogfireProxyCORSMiddleware:
    """Permissive CORS just for ``/logfire-proxy/*``.

    The browser SDK posts OTLP spans cross-origin. ``CORSMiddleware``
    rejects the preflight ``OPTIONS`` with ``400 Disallowed CORS
    origin`` for any origin not enumerated in ``CORS_ALLOWED_ORIGINS``
    — which in practice means every Vercel preview URL fails since
    those subdomains are PR-specific and impossible to enumerate
    ahead of time.

    This shim short-circuits the preflight for the proxy path before
    the main CORS middleware can reject it, and reflects the requesting
    origin on the actual POST response so the browser accepts the
    successful upload. It is **scoped to the proxy path only** — every
    other endpoint still goes through the main allowlist.

    Mount this BEFORE ``CORSMiddleware`` so it is wrapped outermost
    (Starlette executes middleware first-added-first).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not path.startswith("/logfire-proxy/"):
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        origin = headers.get(b"origin", b"").decode("latin-1")

        if scope.get("method") == "OPTIONS":
            requested_headers = headers.get(
                b"access-control-request-headers", b"*"
            ).decode("latin-1")
            resp_headers = [
                (
                    b"access-control-allow-origin",
                    (origin or "*").encode("latin-1"),
                ),
                (b"access-control-allow-methods", b"POST, OPTIONS"),
                (
                    b"access-control-allow-headers",
                    requested_headers.encode("latin-1"),
                ),
                (b"access-control-max-age", b"86400"),
                (b"vary", b"Origin"),
                (b"content-length", b"0"),
            ]
            await send(
                {
                    "type": "http.response.start",
                    "status": 204,
                    "headers": resp_headers,
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return

        async def wrapped_send(message):
            if message["type"] == "http.response.start" and origin:
                resp_headers = list(message.get("headers", []))
                # If the main CORSMiddleware already set ACAO (origin
                # happened to be in the main allowlist), don't add a
                # second one — duplicate ACAO headers are treated as
                # malformed by browsers.
                has_acao = any(
                    name == b"access-control-allow-origin" for name, _ in resp_headers
                )
                if not has_acao:
                    resp_headers.append(
                        (b"access-control-allow-origin", origin.encode("latin-1"))
                    )
                    resp_headers.append((b"vary", b"Origin"))
                message["headers"] = resp_headers
            await send(message)

        await self.app(scope, receive, wrapped_send)


def mount_browser_proxy(app: "FastAPI") -> None:
    """Expose ``/logfire-proxy/{path:path}`` for the browser SDK.

    The proxy reuses the server-side ``LOGFIRE_TOKEN`` to attach the
    Authorization header so it never has to ship to the client.

    **Opt-in only.** The proxy is OFF by default even when
    ``LOGFIRE_TOKEN`` is set, because the browser SDK fires many
    batched POSTs per page load and each one occupies a Modal container
    concurrency slot for ~100-300ms while it forwards to Logfire. On
    bursty front-ends this contends meaningfully with real API traffic.

    Set ``ODDISH_LOGFIRE_BROWSER_PROXY=1`` to opt back in. Server-side
    tracing (FastAPI / asyncpg) is unaffected -- those spans ship
    directly from the backend container, not through this proxy.
    """
    if not _configured:
        return
    if os.environ.get("ODDISH_LOGFIRE_BROWSER_PROXY", "").lower() not in (
        "1",
        "true",
        "yes",
    ):
        logger.info(
            "logfire browser proxy NOT mounted "
            "(set ODDISH_LOGFIRE_BROWSER_PROXY=1 to enable)"
        )
        return
    try:
        from logfire.experimental.forwarding import logfire_proxy
    except Exception:
        logger.warning("logfire browser proxy unavailable", exc_info=True)
        return

    @app.post("/logfire-proxy/{path:path}", include_in_schema=False)
    async def _logfire_browser_proxy(request: Request, path: str):  # noqa: ARG001
        # `Request` MUST resolve through module-level imports for
        # FastAPI to recognise it as the special Starlette Request
        # type. With `from __future__ import annotations` in this
        # file, FastAPI's `typing.get_type_hints` can only resolve
        # names in the module's globals — not function-local imports
        # — so a stray `from fastapi import Request` inside this
        # helper used to break the route with `422 missing field
        # 'request'` (FastAPI fell back to treating it as a query
        # parameter).
        return await logfire_proxy(request)

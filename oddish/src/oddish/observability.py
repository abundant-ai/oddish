"""Optional OpenTelemetry (Logfire) tracing for the portable oddish layer.

Mirrors ``backend/observability.py``'s ``span`` / ``configure`` but lives in the
``oddish`` package (which must not import ``backend/``) and treats logfire as
OPTIONAL: when logfire isn't installed or no ``LOGFIRE_TOKEN`` is set, everything
degrades to a no-op. So the standalone off-Modal dispatcher (``oddish.dispatch.
runner``) emits traces when observability is available and runs unchanged when it
isn't. On Modal the backend configures logfire via ``backend/observability``; this
shim shares the same in-process logfire singleton.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import nullcontext

logger = logging.getLogger(__name__)

_configured = False
_lock = threading.Lock()


def configure_observability(service_name: str) -> bool:
    """Initialize Logfire for a standalone oddish process (e.g. the off-Modal
    dispatcher). No-op returning False when logfire is unavailable or no
    ``LOGFIRE_TOKEN`` is set. Idempotent.
    """
    global _configured
    with _lock:
        if _configured:
            return True
        if not os.environ.get("LOGFIRE_TOKEN"):
            logger.info(
                "LOGFIRE_TOKEN not set; oddish tracing disabled (%s)", service_name
            )
            return False
        try:
            import logfire
        except ImportError:
            logger.info("logfire not installed; oddish tracing disabled")
            return False
        try:
            logfire.configure(
                service_name=service_name,
                service_version=os.environ.get("ODDISH_RELEASE")
                or os.environ.get("GIT_COMMIT_SHA"),
                send_to_logfire="if-token-present",
                console=False,
            )
        except Exception:
            logger.warning("logfire.configure failed", exc_info=True)
            return False
        # Auto-instrument the DB/HTTP the dispatch cycle uses so its work nests
        # under the explicit ``dispatch.cycle`` span (see ``span`` below).
        for name in ("instrument_asyncpg", "instrument_httpx", "instrument_system_metrics"):
            fn = getattr(logfire, name, None)
            if fn is None:
                continue
            try:
                fn()
            except Exception:
                logger.warning("logfire %s failed", name, exc_info=True)
        _configured = True
        logger.info("oddish tracing configured (service=%s)", service_name)
        return True


def span(name: str, /, **attributes):
    """Open a Logfire span, or a no-op context manager when tracing is off.

    Use at portable-layer entry points (the off-Modal dispatch cycle) so the
    auto-instrumented DB/HTTP children nest under a named parent instead of
    floating at the trace root. Gated on ``configure_observability`` having run
    so an unconfigured logfire never warns on the hot path.
    """
    if _configured:
        try:
            import logfire

            return logfire.span(name, **attributes)
        except Exception:
            logger.warning("logfire.span(%r) failed", name, exc_info=True)
    return nullcontext()

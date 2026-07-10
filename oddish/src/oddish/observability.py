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
        for name in (
            "instrument_asyncpg",
            "instrument_httpx",
            "instrument_system_metrics",
        ):
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


def mark_observability_configured() -> None:
    """Record that another package configured the shared Logfire singleton.

    The hosted backend configures Logfire before importing Oddish worker
    modules. This hook lets portable Oddish logging helpers use that existing
    configuration without importing the hosted package or configuring Logfire
    a second time.
    """
    global _configured
    with _lock:
        _configured = True


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


def log_warning(
    message: str,
    *,
    tags: tuple[str, ...] = (),
    **attributes,
) -> None:
    """Emit a warning to process logs and, when configured, Logfire.

    Standard logging remains the durable fallback for self-hosted installs.
    The direct Logfire record is structured so alert rules can group/filter by
    attributes such as ``model`` instead of parsing console text.
    """
    rendered_attributes = " ".join(
        f"{key}={value!r}" for key, value in sorted(attributes.items())
    )
    logger.warning(
        "%s%s",
        message,
        f" {rendered_attributes}" if rendered_attributes else "",
    )
    if not _configured:
        return
    try:
        import logfire

        logfire.warning(message, _tags=list(tags) or None, **attributes)
    except Exception:
        logger.warning("logfire.warning(%r) failed", message, exc_info=True)


def log_unpriced_trial_if_needed(
    *,
    cost_usd: float | None,
    trial_id: str,
    model: str | None,
    agent: str | None,
    provider: str | None,
    attempt: int,
    input_tokens: int | None,
    cache_tokens: int | None,
    cache_write_tokens: int | None,
    output_tokens: int | None,
    native_cost_usd: float | None,
    native_cost_trusted: bool,
) -> bool:
    """Log one structured integrity warning when tokens cannot be priced."""
    if cost_usd is not None or not any(
        int(tokens or 0) > 0
        for tokens in (input_tokens, output_tokens, cache_write_tokens)
    ):
        return False

    log_warning(
        "Trial has token usage but no resolved cost",
        tags=("cost-integrity", "unpriced-model"),
        metric="trial_cost_unpriced",
        trial_id=trial_id,
        model=model or "unknown",
        agent=agent or "unknown",
        provider=provider or "unknown",
        attempt=attempt,
        input_tokens=input_tokens or 0,
        cache_tokens=cache_tokens or 0,
        cache_write_tokens=cache_write_tokens or 0,
        output_tokens=output_tokens or 0,
        native_cost_usd=native_cost_usd,
        native_cost_trusted=native_cost_trusted,
    )
    return True

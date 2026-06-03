"""Worker package.

Configure Logfire here — before importing ``worker.functions`` — so
auto-tracing hooks are installed before the worker handlers and the
``oddish.core`` / ``oddish.queue`` / ``oddish.workers`` modules they
pull in are loaded. See ``api/__init__.py`` for the matching API-side
rationale.

We also wrap the actual import-chain inside a ``worker.container_init``
span so that one-time module-load side effects — `litellm` fetching
its pricing JSON, `ensure_builtin_handlers_registered`, the very
first ``CREATE`` on the SQLAlchemy engine — get parented under a
named root instead of arriving in Logfire as orphan spans on every
container cold start.
"""

from __future__ import annotations

from observability import configure_logfire, span as _otel_span

configure_logfire(service_name="oddish-worker")

with _otel_span("worker.container_init"):
    from .functions import poll_queue, process_single_job  # noqa: E402

__all__ = ["poll_queue", "process_single_job"]

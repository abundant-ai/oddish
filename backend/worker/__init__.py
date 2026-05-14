"""Worker package.

Configure Logfire here — before importing ``worker.functions`` — so
auto-tracing hooks are installed before the worker handlers and the
``oddish.core`` / ``oddish.queue`` / ``oddish.workers`` modules they
pull in are loaded. See ``api/__init__.py`` for the matching API-side
rationale.
"""

from __future__ import annotations

from observability import configure_logfire

configure_logfire(service_name="oddish-worker")

from .functions import poll_queue, process_single_job  # noqa: E402

__all__ = ["poll_queue", "process_single_job"]

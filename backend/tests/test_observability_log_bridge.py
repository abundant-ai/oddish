"""configure_stdlib_log_bridge wires the `oddish` logger to a real sink.

Without the bridge the Modal worker drops every `logging.getLogger("oddish.*")`
INFO record (root at WARNING, no handler), so the analyzer/verdict block's
prefixed progress lines never surface. These tests pin that the bridge gives
that logger tree an INFO-level handler that actually emits, and that it stays
scoped + idempotent.
"""

from __future__ import annotations

import io
import logging

import pytest

import observability


@pytest.fixture
def clean_oddish_logger():
    """Snapshot and restore global logging state so the bridge's mutations
    don't leak into other tests (loggers are process-global singletons)."""
    lg = logging.getLogger("oddish")
    saved_handlers = lg.handlers[:]
    saved_level = lg.level
    saved_propagate = lg.propagate
    saved_flag = observability._log_bridge_configured

    lg.handlers = []
    observability._log_bridge_configured = False
    try:
        yield lg
    finally:
        lg.handlers = saved_handlers
        lg.level = saved_level
        lg.propagate = saved_propagate
        observability._log_bridge_configured = saved_flag


def _stream_handler(lg: logging.Logger) -> logging.StreamHandler:
    handlers = [h for h in lg.handlers if type(h) is logging.StreamHandler]
    assert len(handlers) == 1, f"expected one StreamHandler, got {lg.handlers}"
    return handlers[0]


def test_bridge_emits_oddish_info_with_the_block_prefix(clean_oddish_logger):
    observability.configure_stdlib_log_bridge(logfire_active=False)

    assert clean_oddish_logger.level == logging.INFO

    # Redirect the handler's stream so we can read exactly what it writes,
    # rather than depending on capsys' stderr replacement timing.
    buf = io.StringIO()
    _stream_handler(clean_oddish_logger).stream = buf

    # The real logger name + prefix the analyzer block uses.
    logging.getLogger("oddish.analyzer_block").info(
        "[analyzer/task_verdict] block succeeded (1 chunk(s))"
    )

    out = buf.getvalue()
    assert "[analyzer/task_verdict] block succeeded (1 chunk(s))" in out
    assert "oddish.analyzer_block" in out
    assert "INFO" in out


def test_bridge_is_idempotent(clean_oddish_logger):
    observability.configure_stdlib_log_bridge(logfire_active=False)
    observability.configure_stdlib_log_bridge(logfire_active=False)
    stream_handlers = [
        h for h in clean_oddish_logger.handlers if type(h) is logging.StreamHandler
    ]
    assert len(stream_handlers) == 1


def test_bridge_scoped_to_oddish_not_root(clean_oddish_logger):
    """Third-party INFO (asyncpg/httpx/litellm) must not be pulled in: the
    handler lives on the `oddish` logger and root is left untouched."""
    root_handlers_before = logging.getLogger().handlers[:]
    observability.configure_stdlib_log_bridge(logfire_active=False)
    assert logging.getLogger().handlers == root_handlers_before
    assert clean_oddish_logger.handlers  # but oddish got one


def test_bridge_attaches_logfire_handler_when_active(clean_oddish_logger, monkeypatch):
    import logfire

    made: list[object] = []

    class _FakeLogfireHandler(logging.Handler):
        def __init__(self, *a, **k):
            super().__init__()
            made.append(self)

        def emit(self, record):  # pragma: no cover - not exercised
            pass

    monkeypatch.setattr(logfire, "LogfireLoggingHandler", _FakeLogfireHandler)

    observability.configure_stdlib_log_bridge(logfire_active=True)

    assert len(made) == 1
    assert made[0] in clean_oddish_logger.handlers


def test_bridge_skips_logfire_handler_when_inactive(clean_oddish_logger):
    observability.configure_stdlib_log_bridge(logfire_active=False)
    # Only the stderr StreamHandler, no logfire handler.
    assert all(type(h) is logging.StreamHandler for h in clean_oddish_logger.handlers)

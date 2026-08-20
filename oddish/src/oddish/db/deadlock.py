"""Bounded retry for Postgres deadlock / serialization aborts.

Postgres resolves a deadlock by killing ONE of the two transactions with
SQLSTATE 40P01 (``deadlock_detected``); serialization conflicts surface as
40001 (``serialization_failure``). Both mean "the whole transaction was
rolled back; run it again" -- so the retry boundary MUST be a callable that
opens its own session. A session that lost a deadlock is aborted and
unusable; retrying a single statement inside it can never work.

Observed producer (2026-08-19, production): the trial-import transaction and
a TAG_PROJECT recompute both UPDATE the same ``tasks`` row under load. The
killed side simply lost its work -- an imported trial vanished until a later
import re-ran the stage transition. Every site wrapped with this helper is
idempotent (imports are idempotency-keyed, tag projection recomputes from
truth), so re-running the whole transaction is safe.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)

T = TypeVar("T")

_RETRYABLE_SQLSTATES = frozenset({"40001", "40P01"})


def is_transient_serialization_error(exc: BaseException) -> bool:
    """True when *exc* wraps a Postgres deadlock / serialization abort.

    Walks the wrapper chain (SQLAlchemy ``DBAPIError.orig`` -> the driver
    adapter -> ``__cause__`` -> the asyncpg exception) looking for a retryable
    SQLSTATE, since each layer exposes the code under a different attribute.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        code = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if code in _RETRYABLE_SQLSTATES:
            return True
        nxt = getattr(current, "orig", None)
        if not isinstance(nxt, BaseException):
            nxt = current.__cause__
        current = nxt
    return False


async def run_with_deadlock_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    what: str,
    attempts: int = 3,
    base_delay_seconds: float = 0.1,
) -> T:
    """Run *fn* (which must open its own session), retrying deadlock losses.

    Any exception that is not a Postgres deadlock/serialization abort -- and
    the final losing attempt -- propagates unchanged. Delays back off
    exponentially with jitter so two colliding transactions don't re-collide
    in lockstep.
    """
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except SQLAlchemyError as exc:
            if attempt == attempts or not is_transient_serialization_error(exc):
                raise
            delay = base_delay_seconds * (2 ** (attempt - 1)) * (1.0 + random.random())
            logger.warning(
                "metric=db.deadlock_retry what=%s attempt=%d/%d delay=%.2fs",
                what,
                attempt,
                attempts,
                delay,
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


def retry_deadlocks(
    *, what: str, attempts: int = 3
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Decorator form of :func:`run_with_deadlock_retry`.

    Apply to an async function that opens (and commits/rolls back) its own
    session per call, e.g. a worker-job handler or a request-scoped core
    entry point.
    """

    def wrap(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(fn)
        async def inner(*args: Any, **kwargs: Any) -> T:
            return await run_with_deadlock_retry(
                lambda: fn(*args, **kwargs), what=what, attempts=attempts
            )

        return inner

    return wrap

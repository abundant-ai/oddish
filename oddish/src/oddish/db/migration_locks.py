"""Lock-retry helper for Alembic migrations that run against a live database.

A statement that needs a strong lock (``ALTER TABLE``'s ACCESS EXCLUSIVE,
``DROP TABLE``, or an UPDATE contending on row locks) runs with a short
``lock_timeout`` on purpose: while such a statement waits in the lock queue,
every later query on that table queues behind it, so an uncapped wait stalls
all traffic on the table. The flip side is that one short window can simply
lose the race — the production deploy of ``trialkind01`` lost its single 8s
window on the busy ``trials`` table three runs in a row. This helper keeps
the short window but retries it many times, and on the first miss prints the
sessions holding locks on the table so a failed run's CI log names the
blocker instead of just "lock timeout".

Each attempt runs in its own autocommit transaction, so the helper is only
safe for statements that are individually idempotent — every statement in
the migrations that use it is (``IF [NOT] EXISTS`` DDL, UPDATEs whose WHERE
clause no longer matches after they apply).
"""

from __future__ import annotations

import time
from collections.abc import Callable

import sqlalchemy as sa
from alembic import op

LOCK_TIMEOUT = "5s"
LOCK_RETRY_ATTEMPTS = 60
LOCK_RETRY_PAUSE_SECONDS = 5.0


def _is_lock_timeout(error: sa.exc.DBAPIError) -> bool:
    return "lock timeout" in str(error).lower()


def log_lock_holders(table_name: str) -> None:
    """Print sessions holding locks on ``table_name`` (best-effort)."""
    try:
        rows = (
            op.get_bind()
            .execute(
                sa.text(
                    """
                    SELECT a.pid,
                           a.state,
                           COALESCE(now() - a.xact_start,
                                    now() - a.query_start) AS age,
                           left(a.query, 200) AS query
                    FROM pg_locks l
                    JOIN pg_stat_activity a ON a.pid = l.pid
                    WHERE l.relation = to_regclass(:table_name)
                    ORDER BY 3 DESC NULLS LAST
                    """
                ),
                {"table_name": table_name},
            )
            .fetchall()
        )
    except Exception as introspection_error:  # noqa: BLE001 - diagnostics only
        print(f"could not read lock holders on {table_name}: {introspection_error}")
        return
    for row in rows:
        print(
            f"lock holder on {table_name}: pid={row.pid} state={row.state} "
            f"age={row.age} query={row.query!r}"
        )


def run_with_lock_retry(
    step: Callable[[], None],
    *,
    table_name: str,
    attempts: int = LOCK_RETRY_ATTEMPTS,
    pause_seconds: float = LOCK_RETRY_PAUSE_SECONDS,
) -> None:
    """Run ``step`` in its own autocommit transaction, retrying lock timeouts.

    ``step`` issues the statement(s) itself (via ``op``); any error other
    than a lock timeout propagates immediately, and the final lock timeout
    propagates after the last attempt.
    """
    for attempt in range(1, attempts + 1):
        try:
            with op.get_context().autocommit_block():
                op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
                step()
            return
        except sa.exc.DBAPIError as error:
            if not _is_lock_timeout(error):
                raise
            if attempt == 1:
                log_lock_holders(table_name)
            if attempt == attempts:
                raise
            print(
                f"lock timeout on {table_name} "
                f"(attempt {attempt}/{attempts}); retrying in {pause_seconds}s"
            )
            time.sleep(pause_seconds)

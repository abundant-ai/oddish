"""Narrow Postgres error introspection for deploy-before-migrate tolerance.

A reader that tolerates a not-yet-created table must distinguish "this table does
not exist" from "this query is wrong". Catching every ``ProgrammingError`` hides
real SQL defects behind the same fallback, which is how a degraded read starts
silently returning wrong numbers forever.

The undefined-table SQLSTATE is spelled differently by each driver and sits at
varying depths under ``.orig``, hence the explicit checks. The hosted layer has
its own richer copy in ``backend/pg_errors.py`` (which also covers undefined
COLUMN); this is the OSS-core subset, importable from ``oddish`` alone.
"""

from __future__ import annotations

from sqlalchemy.exc import ProgrammingError

UNDEFINED_TABLE_SQLSTATE = "42P01"


def is_missing_table(exc: BaseException) -> bool:
    """True only for Postgres' undefined-table error, never a generic SQL fault."""
    if not isinstance(exc, ProgrammingError):
        return False
    orig = getattr(exc, "orig", None)
    return (
        getattr(orig, "sqlstate", None) == UNDEFINED_TABLE_SQLSTATE
        or getattr(orig, "pgcode", None) == UNDEFINED_TABLE_SQLSTATE
        or "UndefinedTable" in type(orig).__name__
    )

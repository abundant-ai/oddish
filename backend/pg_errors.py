"""Postgres error introspection shared by callers that tolerate a missing table.

Backend code deploys ahead of its migrations, so anything reading a table added
in the same PR has to answer "does this table exist yet?" without a schema
round-trip. Both known drivers spell the undefined-table SQLSTATE differently
and bury it at varying depths, hence the walk.
"""

from sqlalchemy.exc import ProgrammingError

_UNDEFINED_TABLE_SQLSTATE = "42P01"
_UNDEFINED_COLUMN_SQLSTATE = "42703"


def _matches(
    exc: ProgrammingError, sqlstates: tuple[str, ...], names: tuple[str, ...]
) -> bool:
    error: BaseException | None = exc.orig if exc.orig is not None else exc
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if (
            getattr(error, "sqlstate", None) in sqlstates
            or getattr(error, "pgcode", None) in sqlstates
            or type(error).__name__ in names
        ):
            return True
        error = error.__cause__
    return False


def is_undefined_table_error(exc: ProgrammingError) -> bool:
    return _matches(exc, (_UNDEFINED_TABLE_SQLSTATE,), ("UndefinedTableError",))


def is_undefined_column_or_table_error(exc: ProgrammingError) -> bool:
    """A missing table OR a missing column. Deploy-before-migrate can hit either
    when a query names a not-yet-created table or one of its new columns."""
    return _matches(
        exc,
        (_UNDEFINED_TABLE_SQLSTATE, _UNDEFINED_COLUMN_SQLSTATE),
        ("UndefinedTableError", "UndefinedColumnError"),
    )

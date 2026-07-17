"""Postgres error introspection shared by callers that tolerate a missing table.

Backend code deploys ahead of its migrations, so anything reading a table added
in the same PR has to answer "does this table exist yet?" without a schema
round-trip. Both known drivers spell the undefined-table SQLSTATE differently
and bury it at varying depths, hence the walk.
"""

from sqlalchemy.exc import ProgrammingError

_UNDEFINED_TABLE_SQLSTATE = "42P01"


def is_undefined_table_error(exc: ProgrammingError) -> bool:
    error: BaseException | None = exc.orig if exc.orig is not None else exc
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if (
            getattr(error, "sqlstate", None) == _UNDEFINED_TABLE_SQLSTATE
            or getattr(error, "pgcode", None) == _UNDEFINED_TABLE_SQLSTATE
            or type(error).__name__ == "UndefinedTableError"
        ):
            return True
        error = error.__cause__
    return False

"""``read_optional_table``: savepoint on write sessions, none on read
sessions, ``None`` only for a missing table."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from oddish.db.connection import get_read_session, get_session
from oddish.db.optional_read import read_optional_table


class _Nested:
    def __init__(self):
        self.entered = False

    async def __aenter__(self):
        self.entered = True

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    def __init__(self, *, read: bool):
        self.info = {"oddish_read_autocommit": True} if read else {}
        self.nested = _Nested()

    def begin_nested(self):
        if self.info.get("oddish_read_autocommit"):
            raise AssertionError("read sessions must not open a savepoint")
        return self.nested


class _Undefined(Exception):
    sqlstate = "42P01"


class _Syntax(Exception):
    sqlstate = "42601"


@pytest.mark.asyncio
async def test_write_session_wraps_the_read_in_a_savepoint():
    session = _FakeSession(read=False)

    async def read():
        return "rows"

    assert await read_optional_table(session, read, table="t", degraded="x") == "rows"  # type: ignore[arg-type]
    assert session.nested.entered is True


@pytest.mark.asyncio
async def test_read_session_runs_without_a_savepoint():
    session = _FakeSession(read=True)

    async def read():
        return "rows"

    assert await read_optional_table(session, read, table="t", degraded="x") == "rows"  # type: ignore[arg-type]
    assert session.nested.entered is False


@pytest.mark.asyncio
async def test_missing_table_returns_none_and_other_faults_propagate():
    session = _FakeSession(read=True)

    async def missing():
        raise ProgrammingError("SELECT", {}, _Undefined())

    async def broken():
        raise ProgrammingError("SELEC", {}, _Syntax())

    assert await read_optional_table(session, missing, table="t", degraded="x") is None  # type: ignore[arg-type]
    with pytest.raises(ProgrammingError):
        await read_optional_table(session, broken, table="t", degraded="x")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_missing_table_against_a_real_database_in_both_modes():
    """A failed statement on the read session must not poison the next one,
    and on the write session the savepoint keeps the transaction usable."""

    async def read_missing(session):
        return list(await session.execute(text("SELECT 1 FROM oddish_not_a_table")))

    async with get_read_session() as session:
        assert (
            await read_optional_table(
                session, lambda: read_missing(session), table="t", degraded="x"
            )
            is None
        )
        assert await session.scalar(text("SELECT 1")) == 1

    async with get_session() as session:
        assert (
            await read_optional_table(
                session, lambda: read_missing(session), table="t", degraded="x"
            )
            is None
        )
        assert await session.scalar(text("SELECT 1")) == 1

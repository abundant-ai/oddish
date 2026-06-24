"""Fail when the migrated schema diverges from the create_all schema.

Schema A: empty public schema -> ``alembic upgrade head`` for the oddish stack
then the backend stack (the production migration order).

Schema B: empty public schema -> import backend models + oddish models ->
``Base.metadata.create_all`` (the shape preview builds from the model graph).

Both are introspected with SQLAlchemy ``inspect`` and compared object by object
-- tables, columns, indexes (including partial ``WHERE`` predicates), and
constraints. Any object that exists in one schema but not the other (or whose
shape differs) is a drift and fails the check with a named diff.

A drift here means a migration created a database object with no matching model
/ metadata entry, so create_all-built databases (preview) silently lack it. The
fix is to mirror the object into the model graph (or, for preview, run the
migrations) -- NOT to relax this guard.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import inspect, pool, text
from sqlalchemy.ext.asyncio import create_async_engine

REPO = Path(__file__).resolve().parents[3]
ODDISH = REPO / "oddish"
BACKEND = REPO / "backend"

IGNORED_TABLES = {"alembic_version"}


def _async_url(env_var: str) -> str:
    url = os.environ[env_var]
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def _engine(url: str):
    return create_async_engine(
        url, connect_args={"statement_cache_size": 0}, poolclass=pool.NullPool
    )


async def _reset_public(url: str) -> None:
    engine = _engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
            await conn.execute(text("GRANT ALL ON SCHEMA public TO public"))
    finally:
        await engine.dispose()


def _alembic_upgrade(project: Path, url: str) -> None:
    env = dict(os.environ, ODDISH_DATABASE_URL=url)
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=project,
        env=env,
        check=True,
    )


def _build_migrated(url: str) -> None:
    asyncio.run(_reset_public(url))
    _alembic_upgrade(ODDISH, url)
    _alembic_upgrade(BACKEND, url)


def _load_base():
    for path in (str(BACKEND), str(ODDISH / "src")):
        if path not in sys.path:
            sys.path.insert(0, path)
    import models  # noqa: F401  -- registers cloud tables on the shared Base
    from oddish.db.models import Base

    return Base


async def _create_all(url: str) -> None:
    base = _load_base()
    engine = _engine(url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(base.metadata.create_all)
    finally:
        await engine.dispose()


def _build_metadata(url: str) -> None:
    asyncio.run(_reset_public(url))
    asyncio.run(_create_all(url))


def _snapshot_sync(conn) -> dict:
    insp = inspect(conn)
    out: dict = {}
    for table in insp.get_table_names(schema="public"):
        if table in IGNORED_TABLES:
            continue
        columns = {
            c["name"]: f"{c['type']!s} null={bool(c['nullable'])}"
            for c in insp.get_columns(table, schema="public")
        }
        indexes = {}
        for ix in insp.get_indexes(table, schema="public"):
            where = (ix.get("dialect_options") or {}).get("postgresql_where")
            indexes[ix["name"]] = (
                f"cols={list(ix['column_names'])} "
                f"unique={bool(ix.get('unique'))} where={where}"
            )
        uniques = {
            uc["name"]: list(uc["column_names"])
            for uc in insp.get_unique_constraints(table, schema="public")
        }
        checks = {
            ck["name"]: ck["sqltext"]
            for ck in insp.get_check_constraints(table, schema="public")
        }
        fks = {}
        for fk in insp.get_foreign_keys(table, schema="public"):
            fks[fk.get("name") or str(fk["constrained_columns"])] = (
                f"{list(fk['constrained_columns'])}->"
                f"{fk['referred_table']}{list(fk['referred_columns'])}"
            )
        pk = insp.get_pk_constraint(table, schema="public")
        out[table] = {
            "columns": columns,
            "indexes": indexes,
            "unique_constraints": uniques,
            "check_constraints": checks,
            "foreign_keys": fks,
            "primary_key": list(pk.get("constrained_columns") or []),
        }
    return out


async def _snapshot(url: str) -> dict:
    engine = _engine(url)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(_snapshot_sync)
    finally:
        await engine.dispose()


def _diff(migrated: dict, metadata: dict) -> list[str]:
    problems: list[str] = []

    for t in sorted(set(migrated) - set(metadata)):
        problems.append(
            f"TABLE {t!r}: present after migrations, MISSING from create_all "
            f"(migration DDL has no model/metadata entry)"
        )
    for t in sorted(set(metadata) - set(migrated)):
        problems.append(
            f"TABLE {t!r}: present in create_all, MISSING after migrations"
        )

    for t in sorted(set(migrated) & set(metadata)):
        m, b = migrated[t], metadata[t]
        for kind in (
            "columns",
            "indexes",
            "unique_constraints",
            "check_constraints",
            "foreign_keys",
        ):
            mk, bk = m[kind], b[kind]
            for name in sorted(set(mk) - set(bk)):
                problems.append(
                    f"{kind.upper()} {t}.{name}: present after migrations, "
                    f"MISSING from create_all -> {mk[name]}"
                )
            for name in sorted(set(bk) - set(mk)):
                problems.append(
                    f"{kind.upper()} {t}.{name}: present in create_all, "
                    f"MISSING after migrations -> {bk[name]}"
                )
            for name in sorted(set(mk) & set(bk)):
                if mk[name] != bk[name]:
                    problems.append(
                        f"{kind.upper()} {t}.{name}: differs\n"
                        f"    migrations: {mk[name]}\n"
                        f"    create_all: {bk[name]}"
                    )
        if m["primary_key"] != b["primary_key"]:
            problems.append(
                f"PRIMARY_KEY {t}: migrations={m['primary_key']} "
                f"create_all={b['primary_key']}"
            )
    return problems


def main() -> int:
    migrated_url = _async_url("SCHEMA_PARITY_MIGRATED_URL")
    metadata_url = _async_url("SCHEMA_PARITY_METADATA_URL")

    _build_migrated(migrated_url)
    _build_metadata(metadata_url)

    problems = _diff(asyncio.run(_snapshot(migrated_url)), asyncio.run(_snapshot(metadata_url)))

    if not problems:
        print("schema parity OK: migrations and create_all produce the same schema")
        return 0

    print("SCHEMA DRIFT: the migrated schema and the create_all schema diverge.\n")
    print(
        "Each line below is a database object that one path has and the other\n"
        "lacks (or shapes differently). The common cause is raw op.execute(...)\n"
        "DDL in a migration with no matching model/metadata entry: create_all\n"
        "databases (used by preview) silently lack it. Fix by mirroring the\n"
        "object into the model graph (or switching preview to run migrations) --\n"
        "do not relax this guard.\n"
    )
    for p in problems:
        print(f"  - {p}")
    print(f"\n{len(problems)} drift(s) found.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

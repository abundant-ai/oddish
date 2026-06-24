from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, pool, text
from sqlalchemy.ext.asyncio import create_async_engine

REPO = Path(__file__).resolve().parents[3]
ODDISH = REPO / "oddish"
BACKEND = REPO / "backend"
KINDS = (
    "columns",
    "indexes",
    "unique_constraints",
    "check_constraints",
    "foreign_keys",
)


def _async_url(name: str) -> str:
    url = os.environ[name]
    return (
        url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if url.startswith("postgresql://") and "+asyncpg" not in url
        else url
    )


def _engine(url: str):
    return create_async_engine(
        url, connect_args={"statement_cache_size": 0}, poolclass=pool.NullPool
    )


async def _reset_public(url: str) -> None:
    engine = _engine(url)
    try:
        async with engine.begin() as conn:
            for sql in (
                "DROP SCHEMA IF EXISTS public CASCADE",
                "CREATE SCHEMA public",
                "GRANT ALL ON SCHEMA public TO public",
            ):
                await conn.execute(text(sql))
    finally:
        await engine.dispose()


def _uv_env(project: Path, url: str) -> dict[str, str]:
    env = dict(os.environ, ODDISH_DATABASE_URL=url)
    venv = Path("/opt/venvs") / project.name
    if venv.exists():
        env["UV_PROJECT_ENVIRONMENT"] = str(venv)
    else:
        env.pop("UV_PROJECT_ENVIRONMENT", None)
    return env


def _alembic_upgrade(project: Path, url: str) -> None:
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=project,
        env=_uv_env(project, url),
        check=True,
    )


def _build_migrated(url: str) -> None:
    asyncio.run(_reset_public(url))
    _alembic_upgrade(ODDISH, url)
    _alembic_upgrade(BACKEND, url)


def _load_base():
    sys.path[:0] = [p for p in (str(BACKEND), str(ODDISH / "src")) if p not in sys.path]
    import models
    from oddish.db.models import Base

    _ = models
    return Base


async def _build_metadata(url: str) -> None:
    await _reset_public(url)
    engine = _engine(url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(_load_base().metadata.create_all)
    finally:
        await engine.dispose()


def _index(ix: dict[str, Any]) -> str:
    where = (ix.get("dialect_options") or {}).get("postgresql_where")
    return (
        f"cols={list(ix['column_names'])} unique={bool(ix.get('unique'))} where={where}"
    )


def _fk(fk: dict[str, Any]) -> str:
    return f"{list(fk['constrained_columns'])}->{fk['referred_table']}{list(fk['referred_columns'])}"


def _snapshot_sync(conn) -> dict[str, dict[str, Any]]:
    insp = inspect(conn)
    out = {}
    for table in sorted(
        set(insp.get_table_names(schema="public")) - {"alembic_version"}
    ):
        pk = insp.get_pk_constraint(table, schema="public")
        out[table] = {
            "columns": {
                c["name"]: f"{c['type']!s} null={bool(c['nullable'])}"
                for c in insp.get_columns(table, schema="public")
            },
            "indexes": {
                ix["name"]: _index(ix)
                for ix in insp.get_indexes(table, schema="public")
            },
            "unique_constraints": {
                uc["name"]: list(uc["column_names"])
                for uc in insp.get_unique_constraints(table, schema="public")
            },
            "check_constraints": {
                ck["name"]: ck["sqltext"]
                for ck in insp.get_check_constraints(table, schema="public")
            },
            "foreign_keys": {
                fk.get("name") or str(fk["constrained_columns"]): _fk(fk)
                for fk in insp.get_foreign_keys(table, schema="public")
            },
            "primary_key": list(pk.get("constrained_columns") or []),
        }
    return out


async def _snapshot(url: str) -> dict[str, dict[str, Any]]:
    engine = _engine(url)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(_snapshot_sync)
    finally:
        await engine.dispose()


def _missing(
    problems: list[str],
    kind: str,
    table: str,
    left: dict[str, Any],
    right: dict[str, Any],
    labels: tuple[str, str],
) -> None:
    for name in sorted(set(left) - set(right)):
        problems.append(f"{kind} {table}.{name}: only in {labels[0]} -> {left[name]}")


def _diff(migrated: dict[str, Any], metadata: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for table in sorted(set(migrated) - set(metadata)):
        problems.append(f"TABLE {table}: only in migrations")
    for table in sorted(set(metadata) - set(migrated)):
        problems.append(f"TABLE {table}: only in create_all")
    for table in sorted(set(migrated) & set(metadata)):
        for kind in KINDS:
            a, b = migrated[table][kind], metadata[table][kind]
            _missing(problems, kind.upper(), table, a, b, ("migrations", "create_all"))
            _missing(problems, kind.upper(), table, b, a, ("create_all", "migrations"))
            for name in sorted(set(a) & set(b)):
                if a[name] != b[name]:
                    problems.append(
                        f"{kind.upper()} {table}.{name}: migrations={a[name]} create_all={b[name]}"
                    )
        if migrated[table]["primary_key"] != metadata[table]["primary_key"]:
            problems.append(
                f"PRIMARY_KEY {table}: migrations={migrated[table]['primary_key']} create_all={metadata[table]['primary_key']}"
            )
    return problems


def main() -> int:
    migrated_url = _async_url("SCHEMA_PARITY_MIGRATED_URL")
    metadata_url = _async_url("SCHEMA_PARITY_METADATA_URL")
    _build_migrated(migrated_url)
    asyncio.run(_build_metadata(metadata_url))
    problems = _diff(
        asyncio.run(_snapshot(migrated_url)),
        asyncio.run(_snapshot(metadata_url)),
    )
    if problems:
        print("SCHEMA DRIFT")
        print("\n".join(f"- {problem}" for problem in problems))
        return 1
    print("schema parity OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections import Counter
from importlib import import_module
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import inspect, pool, text
from sqlalchemy.ext.asyncio import create_async_engine

REPO = Path(__file__).resolve().parents[3]
ODDISH = REPO / "oddish"
BACKEND = REPO / "backend"
ALEMBIC_VERSION_TABLES = {
    "alembic_version",
    "alembic_version_backend",
    "alembic_version_oddish",
}
NO_DEFAULT_TIMESTAMP = "type=TIMESTAMP nullable=False default=None identity=None computed=None"
NOW_TIMESTAMP = "type=TIMESTAMP nullable=False default=now() identity=None computed=None"
NO_DEFAULT_BOOLEAN = "type=BOOLEAN nullable=False default=None identity=None computed=None"
TRUE_BOOLEAN = "type=BOOLEAN nullable=False default=true identity=None computed=None"
NO_DEFAULT_VARCHAR_6 = "type=VARCHAR(6) nullable=False default=None identity=None computed=None"
NO_DEFAULT_VARCHAR_16 = "type=VARCHAR(16) nullable=False default=None identity=None computed=None"
NO_DEFAULT_VARCHAR_32 = "type=VARCHAR(32) nullable=False default=None identity=None computed=None"
NO_DEFAULT_VARCHAR_64 = "type=VARCHAR(64) nullable=False default=None identity=None computed=None"
NO_DEFAULT_JSONB = "type=JSONB nullable=False default=None identity=None computed=None"

ALLOWED_VALUE_DIFFS = {
    "COLUMNS chat_session_events.created_at": (NOW_TIMESTAMP, NO_DEFAULT_TIMESTAMP),
    "COLUMNS chat_sessions.created_at": (NOW_TIMESTAMP, NO_DEFAULT_TIMESTAMP),
    "COLUMNS chat_sessions.daytona_session_id": (
        "type=VARCHAR(64) nullable=False default='cc'::character varying identity=None computed=None",
        NO_DEFAULT_VARCHAR_64,
    ),
    "COLUMNS chat_sessions.last_activity": (NOW_TIMESTAMP, NO_DEFAULT_TIMESTAMP),
    "COLUMNS chat_turns.started_at": (NOW_TIMESTAMP, NO_DEFAULT_TIMESTAMP),
    "COLUMNS organizations.created_at": (NOW_TIMESTAMP, NO_DEFAULT_TIMESTAMP),
    "COLUMNS organizations.is_active": (TRUE_BOOLEAN, NO_DEFAULT_BOOLEAN),
    "COLUMNS organizations.plan": (
        "type=VARCHAR(32) nullable=False default='free'::character varying identity=None computed=None",
        NO_DEFAULT_VARCHAR_32,
    ),
    "COLUMNS organizations.settings": (
        "type=JSONB nullable=False default='{}'::jsonb identity=None computed=None",
        NO_DEFAULT_JSONB,
    ),
    "COLUMNS organizations.updated_at": (NOW_TIMESTAMP, NO_DEFAULT_TIMESTAMP),
    "COLUMNS submission_idempotency.created_at": (
        NOW_TIMESTAMP,
        NO_DEFAULT_TIMESTAMP,
    ),
    "COLUMNS submission_idempotency.status": (
        "type=VARCHAR(16) nullable=False default='in_progress'::character varying identity=None computed=None",
        NO_DEFAULT_VARCHAR_16,
    ),
    "COLUMNS submission_idempotency.updated_at": (
        NOW_TIMESTAMP,
        NO_DEFAULT_TIMESTAMP,
    ),
    "COLUMNS users.created_at": (NOW_TIMESTAMP, NO_DEFAULT_TIMESTAMP),
    "COLUMNS users.is_active": (TRUE_BOOLEAN, NO_DEFAULT_BOOLEAN),
    "COLUMNS users.role": (
        "type=VARCHAR(6) nullable=False default='member'::userrole identity=None computed=None",
        NO_DEFAULT_VARCHAR_6,
    ),
    "COLUMNS users.updated_at": (NOW_TIMESTAMP, NO_DEFAULT_TIMESTAMP),
}
ALLOWED_ONLY_IN_MIGRATIONS = {
    "COLUMNS users.supabase_user_id",
    "CONSTRAINTS saved_tag_filters.FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE NOT VALID",
    "CONSTRAINTS saved_tag_filters.FOREIGN KEY (owner_user_id) REFERENCES users(id) ON DELETE CASCADE NOT VALID",
    "CONSTRAINTS tag_assignments.FOREIGN KEY (assigned_by_user_id) REFERENCES users(id) ON DELETE SET NULL NOT VALID",
    "CONSTRAINTS tag_assignments.FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE NOT VALID",
    "CONSTRAINTS tag_events.FOREIGN KEY (actor_user_id) REFERENCES users(id) ON DELETE SET NULL NOT VALID",
    "CONSTRAINTS tag_events.FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE NOT VALID",
    "CONSTRAINTS tag_exclusions.FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL NOT VALID",
    "CONSTRAINTS tag_exclusions.FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE NOT VALID",
    "CONSTRAINTS tag_grants.FOREIGN KEY (granted_by_user_id) REFERENCES users(id) ON DELETE SET NULL NOT VALID",
    "CONSTRAINTS tag_grants.FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE NOT VALID",
    "CONSTRAINTS tag_grants.FOREIGN KEY (principal_user_id) REFERENCES users(id) ON DELETE CASCADE NOT VALID",
    "CONSTRAINTS tag_policies.FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE NOT VALID",
    "CONSTRAINTS tag_policies.FOREIGN KEY (updated_by_user_id) REFERENCES users(id) ON DELETE SET NULL NOT VALID",
    "CONSTRAINTS tags.FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL NOT VALID",
    "CONSTRAINTS tags.FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE NOT VALID",
    "CONSTRAINTS tags.FOREIGN KEY (owner_user_id) REFERENCES users(id) ON DELETE SET NULL NOT VALID",
    "CONSTRAINTS tasks.FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL",
    "CONSTRAINTS tasks.FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE",
    "CONSTRAINTS users.UNIQUE (supabase_user_id)",
    "INDEXES users.idx_users_supabase_user_id",
    "INDEXES users.users_supabase_user_id_key",
}
ALLOWED_ONLY_IN_METADATA: set[str] = set()


def _async_url(name: str) -> str:
    url = os.environ[name]
    return (
        url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if url.startswith("postgresql://") and "+asyncpg" not in url
        else url
    )


def _engine(url: str):
    return create_async_engine(
        url,
        connect_args={"statement_cache_size": 0},
        poolclass=pool.NullPool,
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
    import_module("models")
    from oddish.db.models import Base

    return Base


async def _build_metadata(url: str) -> None:
    await _reset_public(url)
    engine = _engine(url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(_load_base().metadata.create_all)
    finally:
        await engine.dispose()


def _column_shape(column: Mapping[str, Any]) -> str:
    return " ".join(
        (
            f"type={column['type']!s}",
            f"nullable={bool(column['nullable'])}",
            f"default={column.get('default')}",
            f"identity={column.get('identity')}",
            f"computed={column.get('computed')}",
        )
    )


def _table_snapshot(conn, table: str) -> dict[str, dict[str, str]]:
    insp = inspect(conn)
    return {
        "columns": {
            column["name"]: _column_shape(column)
            for column in insp.get_columns(table, schema="public")
        },
        "constraints": _constraint_snapshot(
            conn,
            table,
        ),
        "indexes": _catalog_query(
            conn,
            """
            SELECT i.relname AS name, pg_get_indexdef(i.oid) AS value
            FROM pg_index x
            JOIN pg_class i ON i.oid = x.indexrelid
            JOIN pg_class t ON t.oid = x.indrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname = 'public' AND t.relname = :table
            ORDER BY i.relname
            """,
            {"table": table},
        ),
    }


def _catalog_query(conn, sql: str, params: dict[str, Any] | None = None) -> dict[str, str]:
    rows = conn.execute(text(sql), params or {}).mappings()
    return {row["name"]: row["value"] for row in rows}


def _constraint_snapshot(conn, table: str) -> dict[str, str]:
    rows = conn.execute(
        text(
            """
            SELECT pg_get_constraintdef(c.oid, false) AS value
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname = 'public' AND t.relname = :table
            ORDER BY conname
            """
        ),
        {"table": table},
    ).mappings()
    counts = Counter(row["value"] for row in rows)
    return {value: str(count) for value, count in counts.items()}


def _schema_snapshot_sync(conn) -> dict[str, dict[str, Any]]:
    insp = inspect(conn)
    tables = sorted(set(insp.get_table_names(schema="public")) - ALEMBIC_VERSION_TABLES)
    return {
        "tables": {table: _table_snapshot(conn, table) for table in tables},
        "enums": _catalog_query(
            conn,
            """
            SELECT t.typname AS name, string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder) AS value
            FROM pg_type t
            JOIN pg_enum e ON e.enumtypid = t.oid
            JOIN pg_namespace n ON n.oid = t.typnamespace
            WHERE n.nspname = 'public'
            GROUP BY t.typname
            ORDER BY t.typname
            """,
        ),
    }


async def _schema_snapshot(url: str) -> dict[str, dict[str, Any]]:
    engine = _engine(url)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(_schema_snapshot_sync)
    finally:
        await engine.dispose()


def _compare_map(
    problems: list[str],
    label: str,
    migrated: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> None:
    for name in sorted(set(migrated) - set(metadata)):
        key = f"{label}{name}"
        if key in ALLOWED_ONLY_IN_MIGRATIONS:
            continue
        problems.append(f"{key}: only in migrations -> {migrated[name]}")
    for name in sorted(set(metadata) - set(migrated)):
        key = f"{label}{name}"
        if key in ALLOWED_ONLY_IN_METADATA:
            continue
        problems.append(f"{key}: only in create_all -> {metadata[name]}")
    for name in sorted(set(migrated) & set(metadata)):
        key = f"{label}{name}"
        expected = ALLOWED_VALUE_DIFFS.get(key)
        if expected == (migrated[name], metadata[name]):
            continue
        if migrated[name] != metadata[name]:
            problems.append(f"{key}: migrations={migrated[name]} create_all={metadata[name]}")


def _diff(migrated: dict[str, Any], metadata: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    migrated_tables = migrated["tables"]
    metadata_tables = metadata["tables"]
    _compare_map(problems, "ENUM ", migrated["enums"], metadata["enums"])
    for table in sorted(set(migrated_tables) - set(metadata_tables)):
        problems.append(f"TABLE {table}: only in migrations")
    for table in sorted(set(metadata_tables) - set(migrated_tables)):
        problems.append(f"TABLE {table}: only in create_all")
    for table in sorted(set(migrated_tables) & set(metadata_tables)):
        for kind in ("columns", "constraints", "indexes"):
            _compare_map(
                problems,
                f"{kind.upper()} {table}.",
                migrated_tables[table][kind],
                metadata_tables[table][kind],
            )
    return problems


def main() -> int:
    migrated_url = _async_url("SCHEMA_PARITY_MIGRATED_URL")
    metadata_url = _async_url("SCHEMA_PARITY_METADATA_URL")
    _build_migrated(migrated_url)
    asyncio.run(_build_metadata(metadata_url))
    problems = _diff(
        asyncio.run(_schema_snapshot(migrated_url)),
        asyncio.run(_schema_snapshot(metadata_url)),
    )
    if problems:
        print("SCHEMA DRIFT")
        print("\n".join(f"- {problem}" for problem in problems))
        return 1
    print("schema parity OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
